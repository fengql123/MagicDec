import time
import torch
import sys
sys.path.append("..")
from pathlib import Path
import torch.distributed as dist
from MagicDec.Engine.utils import setup_seed, cuda_graph_for_sampling_argmax_batch, sampling_argmax_batch
from MagicDec.Data.data_converter import convert_pg19_dataset
from transformers import AutoTokenizer
from torch.utils.data.dataloader import DataLoader
from tqdm import tqdm
import argparse
from MagicDec.Engine.StreamingLLM.backend import LMBackend
from datasets import load_dataset
import re
import subprocess as sp
import os

def get_gpu_memory():
    command = "nvidia-smi --query-gpu=memory.free --format=csv"
    memory_free_info = sp.check_output(command.split()).decode('ascii').split('\n')[:-1][1:]
    memory_free_values = [int(x.split()[0]) for i, x in enumerate(memory_free_info)]
    return memory_free_values

parser = argparse.ArgumentParser(description='Process model configuration and partitions.')
parser.add_argument('--model', type=Path, default=Path("/scratch/models/meta-llama/Meta-Llama-3.1-8B/model.pth"), help='model')
parser.add_argument('--model_name', type=str, default="meta-llama/Meta-Llama-3.1-8B", help='model name')
parser.add_argument('--dataset', type=str, default="pg19", help='Dataset name.')
parser.add_argument('--draft_budget', type=int, default=4097, help='Dataset end index.')
parser.add_argument('--rank_group', nargs='+', type=int, help='Target group of ranks')
parser.add_argument('--compile', action='store_true', help='Whether to compile the model.')

parser.add_argument('--gamma', type=int, default=7, help='start')

parser.add_argument('--B', type=int, default=45, help='Batch size.')
parser.add_argument('--prefix_len', type=int, default=100000, help='Prefix length')
parser.add_argument('--max_len', type=int, default=100096, help='Generate length')

parser.add_argument('--seed', type=int, default=123, help='Random seed.')

parser.add_argument('--printoutput', action='store_true', help='Whether to compile the model.')
parser.add_argument('--benchmark', action='store_true', help='Whether to compile the model.')

args = parser.parse_args()
assert args.prefix_len < args.max_len
assert (args.max_len + 127) // 128 == args.prefix_len // 128 + 1
assert (args.draft_budget - 1) % 128 == 0

# Init model parallelism
DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
global print
from MagicDec.Engine.tp import init_dist
use_tp = len(args.rank_group) > 1
global_group = None
rank = 0
if use_tp:
    rank, global_group = init_dist()
    if rank != args.rank_group[0]:
        print = lambda *args, **kwargs: None

# if rank == 0:
#     with open("result.txt", "a") as file:
#         file.write(f"Selfspec: Prefix:{args.prefix_len}; Bsz:{args.B}; Gamma:{args.gamma}; Draft budget:{args.draft_budget}\n")


setup_seed(args.seed)
print(f"Using device={DEVICE}")

MAX_LEN_TARGET = args.max_len
DTYPE = torch.bfloat16
BATCH_SIZE = args.B
benchmark = args.benchmark
checkpoint_path = args.model

target_dec_len = args.gamma + 1

# Load target model
# engine = LMBackend(dtype=DTYPE, device=DEVICE, dec_len=target_dec_len)
# engine.load_model(checkpoint_path, use_tp=use_tp, rank_group = args.rank_group, group=global_group)
# vocab_size = engine.model.config.vocab_size
# if args.compile:
#     engine.compile()
# engine.setup_caches(max_batch_size=BATCH_SIZE, max_seq_length=MAX_LEN_TARGET, draft_budget=args.draft_budget)

# Load dataset
tokenizer = AutoTokenizer.from_pretrained(args.model_name)
tokenizer.pad_token = tokenizer.eos_token
eot_1 = tokenizer.eos_token_id
if tokenizer.unk_token_id is not None:
    eot_2 = tokenizer.unk_token_id
else:
    eot_2 = tokenizer.encode("<|eot_id|>")[-1]
print(f"eot_1: {eot_1}, eot_2: {eot_2}")

# if args.dataset == "pg19":
#     ds = convert_pg19_dataset(tokenizer=tokenizer, seq_len=args.prefix_len)
# # elif args.dataset.startswith("ruler"):
# #     dataset = convert_ruler_dataset(tokenizer=tokenizer, task=args.dataset.split(":")[1], model_name=args.model_name, seq_len=args.prefix_len)
# else:
#     raise ValueError(f"Unknown dataset {args.dataset}")
# dataloader = DataLoader(dataset, batch_size=BATCH_SIZE, shuffle=False, drop_last=True)
ds = load_dataset('THUDM/LongBench-v2', split='train')
# ds = dataloader
num_eval_steps = len(ds)

total_time = 0.0
num_gen_tokens = 0
target_steps = 0
if benchmark:
    draft_time = 0.0
    target_time = 0.0
    verify_loop = 0.0
    

query_template = open('/home/e/e0969258/Projects/MagicDec/tests/prompt_templates/query.txt', encoding='utf-8').read()

def extract_answer(response):
    response = response.replace('*', '')
    match = re.search(r'The correct answer is \(([A-D])\)', response)
    if match:
        return match.group(1)
    else:
        match = re.search(r'The correct answer is ([A-D])', response)
        if match:
            return match.group(1)
        else:
            return None

acc = 0
for step, item in tqdm(enumerate(ds), total=num_eval_steps):
    engine = LMBackend(dtype=DTYPE, device=DEVICE, dec_len=target_dec_len)
    engine.load_model(checkpoint_path, use_tp=use_tp, rank_group = args.rank_group, group=global_group)
    vocab_size = engine.model.config.vocab_size
    if args.compile:
        engine.compile()
    engine.setup_caches(max_batch_size=BATCH_SIZE, max_seq_length=MAX_LEN_TARGET, draft_budget=args.draft_budget)
    
    if step >= num_eval_steps:
        break
    long_context = item["context"]
    query = query_template.replace('$Q$', item['question'].strip()).replace('$C_A$', item['choice_A'].strip()).replace('$C_B$', item['choice_B'].strip()).replace('$C_C$', item['choice_C'].strip()).replace('$C_D$', item['choice_D'].strip())
    
    input_ids = tokenizer([long_context + "\n\n" + query], return_tensors="pt", add_special_tokens=False).input_ids.to(DEVICE)
    # input_ids = item.to(DEVICE)
    terminal = False
    tokens_buffer = torch.zeros((BATCH_SIZE, args.gamma+1), device=DEVICE).long()
    output = torch.zeros(BATCH_SIZE, MAX_LEN_TARGET+1, device=DEVICE).long()
    if input_ids.shape[1] > args.prefix_len:
        input_ids = input_ids[:, -args.prefix_len+1:]
    output[:, :input_ids.shape[1]] = input_ids
    num_nodes = torch.zeros(BATCH_SIZE,device=DEVICE).long()
    num_nodes += input_ids.shape[1]

    # prefill
    start_prefill = time.perf_counter()
    tokens_buffer[:, :1] = engine.encode(input_ids=input_ids)[:,-1:]
    engine.draft_encode(input_ids=input_ids)
    end_prefill = time.perf_counter()

    # decoding
    next_double = False
    double_buffer = None
    cachelens_update = None
    torch.cuda.synchronize()
    start = time.perf_counter()
    while terminal == False:

        # Draft speculation
        if benchmark:
            torch.cuda.synchronize()
            t1 = time.time()

        for i in range(args.gamma):
            if i == 0:
                if next_double:
                    # The cachelens should increase 1 or 2
                    next_tokens = engine.speculate(double_buffer, cachelen_update=cachelens_update)
                    tokens_buffer[:,i+1:i+2] = next_tokens.gather(1, cachelens_update.view(-1,1) - 1)
                    next_double = False
                else:
                    tokens_buffer[:,i+1:i+2] = engine.speculate(tokens_buffer[:, i].view(-1,1))
                continue
            tokens_buffer[:,i+1:i+2] = engine.speculate(tokens_buffer[:, i].view(-1,1))

        # for i in range(args.gamma):
        #     # tokens_buffer[:,i+1:i+2] = draft_sample(engine.speculate(tokens_buffer[:, i].view(-1,1)))
        #     tokens_buffer[:,i+1:i+2] = engine.speculate(tokens_buffer[:, i].view(-1,1))

        if benchmark:
            torch.cuda.synchronize()
            t2 = time.time()
            draft_time+=t2-t1

        # Target Verification
        target_tokens = engine.verify(tokens_buffer)

        if benchmark:
            torch.cuda.synchronize()
            t3 = time.time()
            target_time+=t3-t2

        target_steps+=1

    # Verification
        # Vectorized Verify Loop
        draft_tokens = tokens_buffer[:, 1:args.gamma+1]
        flag_accept_matrix = (target_tokens[:, :args.gamma] == draft_tokens)  # shape: (BATCH_SIZE, gamma)
        eot_condition = ((draft_tokens == eot_1) | (draft_tokens == eot_2))  # shape: (BATCH_SIZE, gamma)

        # Compute accept_flags by considering both the acceptance condition and EOT tokens
        accept_flags_int = (flag_accept_matrix & (~eot_condition)).int()
        accept_flags_cumprod = torch.cumprod(accept_flags_int, dim=1)
        accept_flags_matrix = accept_flags_cumprod.bool()

        # Compute the number of accepted tokens
        accept_nums = accept_flags_matrix.sum(dim=1, keepdim=True) + 1  # shape: (BATCH_SIZE, 1)

        # Check for termination conditions
        condition = (eot_condition & accept_flags_matrix).any(dim=1, keepdim=True)
        if condition.any():
            terminal = True
        
        # Rollback the memory length
        engine.cachelens = engine.cachelens - args.gamma - 1
        engine.paged_kv_last_page_len = engine.paged_kv_last_page_len - args.gamma - 1

        # Put the accepted tokens to output
        positions = torch.arange(output.shape[1], device=DEVICE).view(1, -1).repeat(BATCH_SIZE, 1)
        mask = (positions < (engine.cachelens.view(-1,1) + accept_nums)) & (positions >= engine.cachelens.view(-1, 1))
        positions_buffer = torch.arange(args.gamma+1, device=DEVICE).view(1, -1).repeat(BATCH_SIZE, 1)
        mask_buffer = positions_buffer<accept_nums.view(-1,1)
        output[mask] = tokens_buffer[mask_buffer]

        # Set the cache length to the accepted length
        engine.cachelens += accept_nums.flatten().to(torch.int32)
        engine.paged_kv_last_page_len += accept_nums.flatten().to(torch.int32)

        max_limit = torch.full_like(accept_nums, args.gamma, device = DEVICE)
        limited_accept_nums = torch.min(accept_nums, max_limit)
        
        engine.draft_cachelens = engine.draft_cachelens - args.gamma
        engine.draft_paged_kv_last_page_len = engine.draft_paged_kv_last_page_len - args.gamma
        engine.draft_cachelens += limited_accept_nums.flatten().to(torch.int32)
        engine.draft_paged_kv_last_page_len += limited_accept_nums.flatten().to(torch.int32)
        
        # Get the bonus tokens
        indices = accept_nums - 1
        bonus_tokens = target_tokens.gather(1, indices)
        if (bonus_tokens == eot_1).any() or (bonus_tokens == eot_2).any():
            terminal = True
        num_nodes += accept_nums.flatten()

        # Check Number of Nodes + Bonus Token <= max_target_token
        # if num_nodes.max() + 1 >= args.prefix_len + gen_len:
        # if num_nodes.max() + 1 + args.gamma > MAX_LEN_TARGET:
        if num_nodes.max() - args.prefix_len >= 80:
            terminal = True
        # Put Bonus tokens to the tokens buffer, and prepare the variables for next itr
        if not terminal:
            tokens_buffer[:, :1] = bonus_tokens
            if accept_nums.max() == args.gamma + 1:
                next_double = True
                double_buffer = torch.zeros((BATCH_SIZE, 2), device=DEVICE).long()
                mask = (accept_nums == (args.gamma + 1)).squeeze()
                double_buffer[:, 0] = torch.where(mask, tokens_buffer[:, -1], bonus_tokens[:, 0])
                double_buffer[:, 1] = torch.where(mask, bonus_tokens[:, 0], torch.full((BATCH_SIZE,), -100, device=bonus_tokens.device))
                non_zero_mask = double_buffer != -100
                double_buffer[:, 1] = torch.where(mask, bonus_tokens[:, 0], torch.zeros_like(bonus_tokens[:, 0]))
                cachelens_update = non_zero_mask.sum(dim=1).flatten()
        
        if not terminal:
            if benchmark:
                torch.cuda.synchronize()
                t4 = time.time()
                verify_loop += t4-t3
        else:
            for i in range(BATCH_SIZE):
                output[i, num_nodes[i]] = bonus_tokens[i]
            num_nodes += 1
            if benchmark:
                torch.cuda.synchronize()
                t4 = time.time()
                verify_loop += t4-t3
    
    engine.clear()
    del engine
    torch.cuda.empty_cache()

    torch.cuda.synchronize()
    end=time.perf_counter()
    total_time += end-start
    num_gen_tokens += (num_nodes.sum() - (input_ids.shape[1] + 1) * BATCH_SIZE)
    # if args.printoutput:
    #     for i in range(BATCH_SIZE):
    #         print("Sequence ", i)
    #         print(tokenizer.decode(output[i, args.prefix_len:num_nodes[i]]))
    generated = tokenizer.decode(output[0, args.prefix_len:num_nodes[0]])
    
    print(generated)
    answer = extract_answer(generated)
    if answer == item['answer']:
        acc += 1
    
    print("total time :{:.5f}s, time per iter :{:.5f}s, prefill time :{:.5f}s ,decoding step: {}, large model step: {}".format(total_time, total_time / target_steps, end_prefill - start_prefill, num_gen_tokens, target_steps))
    if benchmark:
        print("target time :{:.5f}s, draft time :{:.5f}s, verify loop : {}, avg generate len per sentence: {}".format(target_time/target_steps, draft_time / target_steps, verify_loop/target_steps, num_gen_tokens/target_steps/BATCH_SIZE))
    if step < 5:   # TODO: revert to 10?
        total_time = 0.0
        num_gen_tokens = 0
        target_steps = 0
        if benchmark:
            draft_time = 0.0
            target_time = 0.0
            verify_loop = 0.0
    if use_tp:
        dist.barrier()

print(f"Accuracy: {acc/len(ds)}")
print(f"Final tokens per second :{num_gen_tokens/total_time}")

# if rank == 0:
#     with open("result.txt", "a") as file:
#         file.write("total time :{:.5f}s, time per iter :{:.5f}s, decoding step: {}, large model step: {}, avg latency: {} \n".format(total_time, total_time / target_steps, num_gen_tokens, target_steps, total_time / num_gen_tokens * BATCH_SIZE))
#         file.write("target time :{:.5f}s, draft time :{:.5f}s, verify loop : {}, avg generate len per sentence: {} \n".format(target_time/target_steps, draft_time / target_steps, verify_loop/target_steps, num_gen_tokens/target_steps/BATCH_SIZE))