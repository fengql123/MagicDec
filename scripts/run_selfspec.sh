model=meta-llama/Meta-Llama-3.1-8B

TASKS=(
    "niah_single_1"
    "niah_single_2"
    "niah_single_3"
    "niah_multikey_1"
    "niah_multikey_2"
    "niah_multikey_3"
    "niah_multivalue"
    "niah_multiquery"
    "vt"
    "cwe"
    "fwe"
    "qa_1"
    "qa_2"
)

gen_len=(
    128
    128
    128
    128
    128
    128
    128
    128
    30
    120
    50
    32
    32
)

prefill=$1
draft_budget=$2
bsz=$3
gamma=$4
MODEL_ROOT=$5

for task_id in {0..12}; do
    TASK=${TASKS[$task_id]}
    gen_len=${gen_len[$task_id]}
    
    # upper clamp gen_len to 96
    gen_len=$((gen_len > 96 ? 96 : gen_len)) 
    max_len=$((prefill + gen_len))
    echo "TASK: ${TASK}"
    echo "gen_len: ${gen_len}"

    torchrun --standalone --nproc_per_node=1 \
    tests/selfspec_benchmark.py \
        --model ${MODEL_ROOT}/${model}/model.pth --model_name ${model} \
        --draft_budget ${draft_budget} --rank_group 0 \
        --gamma ${gamma} --B ${bsz} --prefix_len ${prefill} --max_len ${max_len} \
        --printoutput --benchmark --dataset ruler:${TASK} 
    # -compile
done