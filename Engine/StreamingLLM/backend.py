import torch
from MagicDec.Engine.StreamingLLM.model import Transformer
from MagicDec.Engine.utils import load_model_streamingLLM
import flashinfer

class LMBackend:
    def __init__(self, dtype = torch.bfloat16, device: str = "cuda:0", dec_len: int = 1) -> None:
        self.dtype = dtype
        self.device = device
        self.dec_len = dec_len
        self.cachelens = None
        self.draft_cachelens = None
        self.prefill = lambda model, x, input_pos, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen: model.prefill(x, input_pos, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen)
        self.draft_prefill = lambda model, x, input_pos, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen, is_last=None: model.draft_prefill(x, input_pos, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen, is_last)
        self.model_forward = lambda model, x, input_pos, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen: model.verify(x, input_pos, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen)
        self.draft_forward = {}
        for decode_len in [1,2]:
            self.draft_forward[decode_len] = lambda model, x, input_pos, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen: model.draft_forward(x, input_pos, kv_append_indptr, kv_page_indices, kv_page_indptr, kv_page_lastlen)

    def load_model(self, checkpoints: str, use_tp: bool, rank_group=None, group = None):
        self.model: Transformer = load_model_streamingLLM(checkpoint_path=checkpoints, device=self.device, precision=self.dtype, use_tp=use_tp, rank_group=rank_group, group=group)        

    @torch.inference_mode()
    def setup_caches(self, max_batch_size: int = 1, max_seq_length: int = 2048, draft_budget = 0):
        self.draft_budget = draft_budget
        self.batch_size = max_batch_size
        self.cachelens = torch.zeros(max_batch_size, dtype=torch.int32, device=self.device)
        page_size = 128
        max_num_pages = max_batch_size * max_seq_length // page_size
        if max_num_pages*page_size < max_batch_size*max_seq_length:
            max_num_pages += max_batch_size
        self.max_num_pages_per_request = max_num_pages // max_batch_size
        self.num_pages_per_request = torch.zeros(max_batch_size, device=self.device, dtype=torch.int32)
        self.page_size = page_size
        self.max_num_pages = max_num_pages


        # Init Target Attention Backend(Flashinfer)
        self.prefill_buffer = torch.empty(128 * 1024 * 1024, dtype=torch.uint8, device=self.device)        
        self.prefill_wrapper = flashinfer.BatchPrefillWithPagedKVCacheWrapper(self.prefill_buffer, "NHD")
        
        torch.library.define(
            "mylib::target_prefill",
            "(Tensor q, Tensor kv_cache) -> Tensor",
        )
        @torch.library.impl("mylib::target_prefill", "cuda")
        def target_prefill(q, kv_cache):
            return self.prefill_wrapper.run(
                q, kv_cache
            )
        @torch.library.register_fake("mylib::target_prefill")
        def target_prefill_abstract(q, kv_cache):
            return torch.empty_like(q)
        
        self.decode_buffer = torch.empty(128 * 1024 * 1024, dtype=torch.uint8, device=self.device)
        self.qo_indptr = torch.arange(max_batch_size+1, dtype=torch.int32, device=self.device)
        self.paged_kv_indptr = torch.arange(max_batch_size+1, dtype=torch.int32, device=self.device)
        self.paged_kv_indices = torch.empty(max_num_pages, dtype=torch.int32, device=self.device)
        self.paged_kv_last_page_len = torch.zeros((max_batch_size), dtype=torch.int32, device=self.device)
        self.decode_wrapper = flashinfer.BatchPrefillWithPagedKVCacheWrapper(self.decode_buffer, "NHD", use_cuda_graph=True,
                                                                              qo_indptr_buf=self.qo_indptr, 
                                                                              paged_kv_indptr_buf=self.paged_kv_indptr, 
                                                                              paged_kv_indices_buf=self.paged_kv_indices, 
                                                                              paged_kv_last_page_len_buf=self.paged_kv_last_page_len)
        torch.library.define(
            "mylib::target_decode",
            "(Tensor q, Tensor kv_cache) -> Tensor",
        )
        @torch.library.impl("mylib::target_decode", "cuda")
        def target_decode(q, kv_cache):
            return self.decode_wrapper.run(
                q, kv_cache
            )
        @torch.library.register_fake("mylib::target_decode")
        def target_decode_abstract(q, kv_cache):
            return torch.empty_like(q)

        self.draft_cachelens = torch.zeros(max_batch_size, dtype=torch.int32, device=self.device)
        self.draft_buffer = torch.empty(128 * 1024 * 1024, dtype=torch.uint8, device=self.device)
        self.draft_max_num_pages = (draft_budget//page_size + 1)*max_batch_size
        self.draft_max_num_pages_per_request = self.draft_max_num_pages // max_batch_size
        self.draft_num_pages_per_request = torch.zeros(max_batch_size, device=self.device, dtype=torch.int32)
        self.draft_paged_kv_indptr = torch.arange(max_batch_size+1, dtype=torch.int32, device=self.device)*(draft_budget//page_size + 1)
        self.draft_paged_kv_indices = torch.empty(self.draft_max_num_pages, dtype=torch.int32, device=self.device)
        self.draft_paged_kv_last_page_len = torch.zeros((max_batch_size), dtype=torch.int32, device=self.device)
        self.draft_wrapper = flashinfer.BatchPrefillWithPagedKVCacheWrapper(self.draft_buffer, "NHD", use_cuda_graph=True,
                                                                            qo_indptr_buf=self.qo_indptr, 
                                                                            paged_kv_indptr_buf=self.draft_paged_kv_indptr, 
                                                                            paged_kv_indices_buf=self.draft_paged_kv_indices, 
                                                                            paged_kv_last_page_len_buf=self.draft_paged_kv_last_page_len)
        torch.library.define(
            "mylib::draft_decode",
            "(Tensor q, Tensor kv_cache) -> Tensor",
        )
        @torch.library.impl("mylib::draft_decode", "cuda")
        def draft_decode(q, kv_cache):
            return self.draft_wrapper.run(
                q, kv_cache
            )
        @torch.library.register_fake("mylib::draft_decode")
        def draft_decode_abstract(q, kv_cache):
            return torch.empty_like(q)

        with torch.device(self.device):
            self.model.setup_caches(num_pages=max_num_pages, page_size=page_size, draft_num_pages = self.draft_max_num_pages, draft_budget = draft_budget)

    def compile(self):
        import torch._dynamo.config
        import torch._inductor.config
        torch._inductor.config.coordinate_descent_tuning = True
        torch._inductor.config.triton.unique_kernel_names = True
        torch._inductor.config.fx_graph_cache = True
        torch._functorch.config.enable_autograd_cache = True
        self.model_forward = torch.compile(self.model_forward, mode="max-autotune", fullgraph=True)
        for key in self.draft_forward.keys():
            self.draft_forward[key] = torch.compile(self.draft_forward[key], mode="max-autotune", fullgraph=True)
    
    # Only used for target verification
    @torch.inference_mode()
    def verify(self, input_ids: torch.LongTensor, benchmark = False):
            dec_len = input_ids.shape[1]
            self.pre_verify(dec_len=dec_len)

            logits = self.model_forward(
                model=self.model, 
                x=input_ids,
                input_pos=self.cachelens,
                kv_append_indptr = self.qo_indptr*dec_len, kv_page_indices = self.paged_kv_indices, kv_page_indptr= self.paged_kv_indptr, kv_page_lastlen = self.paged_kv_last_page_len,
                )
            
            self.cachelens += dec_len
            if benchmark:
                # If benchmarking the latency, don't update the cachelens and page table
                self.cachelens -= dec_len
                self.paged_kv_last_page_len -= dec_len
            return logits
    
    def pre_verify(self, dec_len):
            self.paged_kv_last_page_len += dec_len
            self.decode_wrapper.plan(
                qo_indptr=self.qo_indptr*dec_len,
                paged_kv_indptr=self.paged_kv_indptr,
                paged_kv_indices=self.paged_kv_indices,
                paged_kv_last_page_len=self.paged_kv_last_page_len,
                num_qo_heads=self.model.config.n_head, 
                num_kv_heads=self.model.config.n_local_heads, 
                head_dim=self.model.config.head_dim, 
                page_size=self.page_size, 
                q_data_type=self.dtype, 
                causal=True,
            )
    
    @torch.inference_mode()
    def speculate(self, input_ids: torch.LongTensor, benchmark = False, cachelen_update = None):
            dec_len = input_ids.shape[1]
            self.pre_spec(dec_len=dec_len)
            logits = self.draft_forward[dec_len](
                model=self.model, 
                x=input_ids,
                input_pos=self.draft_cachelens, 
                kv_append_indptr = self.qo_indptr*dec_len, kv_page_indices = self.draft_paged_kv_indices, kv_page_indptr= self.draft_paged_kv_indptr, kv_page_lastlen = self.draft_paged_kv_last_page_len)
            
            if cachelen_update == None:
                self.draft_cachelens += dec_len
            else:
                self.draft_cachelens += cachelen_update.to(torch.int32)
                self.draft_paged_kv_last_page_len = self.draft_paged_kv_last_page_len - dec_len + cachelen_update.to(torch.int32).flatten()

            if benchmark:
                # If benchmarking the latency, don't update the cachelens and page table
                self.draft_cachelens -= dec_len
                self.draft_paged_kv_last_page_len -= dec_len
            return logits
    
    def pre_spec(self, dec_len):
            self.draft_paged_kv_last_page_len += dec_len
            self.draft_wrapper.plan(
                qo_indptr=self.qo_indptr*dec_len,
                paged_kv_indptr=self.draft_paged_kv_indptr,
                paged_kv_indices=self.draft_paged_kv_indices,
                paged_kv_last_page_len=self.draft_paged_kv_last_page_len,
                num_qo_heads=self.model.config.n_head, 
                num_kv_heads=self.model.config.n_local_heads, 
                head_dim=self.model.config.head_dim, 
                page_size=self.page_size, 
                q_data_type=self.dtype, 
                causal=True,
            )
    
    @torch.inference_mode()
    def encode(self, input_ids: torch.LongTensor, benchmark = False):
        self.clear_kv()
        logits = None
        seq_len = input_ids.shape[1]
        chunk_size = 128
        num_chunks = (seq_len + chunk_size - 1) // chunk_size  # Ceil division
        for i in range(num_chunks):
            start_idx = i * chunk_size
            end_idx = min((i + 1) * chunk_size, seq_len)
            chunk_input_ids = input_ids[:, start_idx:end_idx]
            dec_len = end_idx-start_idx
            self.pre_encode(dec_len=dec_len)                
            # if not benchmark:
            logits = self.prefill(
                model=self.model,
                x=chunk_input_ids,
                input_pos=self.cachelens,
                kv_append_indptr = self.qo_indptr*dec_len, kv_page_indices = self.paged_kv_indices, kv_page_indptr= self.paged_kv_indptr, kv_page_lastlen = self.paged_kv_last_page_len
            )
            self.cachelens += dec_len
            
        return logits
    
    def pre_encode(self, dec_len):
        self.num_pages_per_request+=1
        qo_indptr = self.qo_indptr*dec_len
        self.paged_kv_indices = torch.cat([torch.arange(i * self.max_num_pages_per_request, i * self.max_num_pages_per_request + self.num_pages_per_request[i], dtype=torch.int32, device=self.device) for i in range(self.batch_size)])
        self.paged_kv_indptr[1:] = torch.cumsum(self.num_pages_per_request, dim=0, dtype=torch.int32)
        self.paged_kv_last_page_len = torch.full((self.batch_size,), dec_len, dtype=torch.int32, device=self.device)
        self.prefill_wrapper.plan(
            qo_indptr=qo_indptr,
            paged_kv_indptr=self.paged_kv_indptr,
            paged_kv_indices=self.paged_kv_indices,
            paged_kv_last_page_len=self.paged_kv_last_page_len,
            num_qo_heads=self.model.config.n_head, 
            num_kv_heads=self.model.config.n_local_heads, 
            head_dim=self.model.config.head_dim, 
            page_size=self.page_size, 
            q_data_type=self.dtype, 
            causal=True
            ) 
    
    @torch.inference_mode()
    def draft_encode(self, input_ids: torch.LongTensor, benchmark = False):
        logits = None
        seq_len = input_ids.shape[1]
        chunk_size = 128
        num_chunks = (seq_len + chunk_size - 1) // chunk_size  # Ceil division
        is_last = False
        for i in range(num_chunks):
            start_idx = i * chunk_size
            end_idx = min((i + 1) * chunk_size, seq_len)
            chunk_input_ids = input_ids[:, start_idx:end_idx]
            dec_len = end_idx-start_idx
            if dec_len != chunk_size:
                is_last = True
            self.pre_draft_encode(dec_len=dec_len) 

            logits = self.draft_prefill(
                model=self.model,
                x=chunk_input_ids,
                input_pos=self.draft_cachelens,
                kv_append_indptr = self.qo_indptr*dec_len, kv_page_indices = self.draft_paged_kv_indices, kv_page_indptr= self.draft_paged_kv_indptr, kv_page_lastlen = self.draft_paged_kv_last_page_len, 
                is_last=is_last
            )
            self.draft_cachelens += dec_len
            if self.draft_cachelens[0] >= self.draft_budget:
                self.draft_cachelens.fill_(self.draft_budget).to(torch.int32)
            
        return logits
    
    def pre_draft_encode(self, dec_len):
        if self.draft_cachelens[0] + dec_len <= self.draft_budget:
            qo_indptr = self.qo_indptr*dec_len
            self.draft_num_pages_per_request+=1
            self.draft_paged_kv_indices = torch.cat([torch.arange(i * self.draft_max_num_pages_per_request, i * self.draft_max_num_pages_per_request + self.draft_num_pages_per_request[i], dtype=torch.int32, device=self.device) for i in range(self.batch_size)])
            self.draft_paged_kv_indptr[1:] = torch.cumsum(self.draft_num_pages_per_request, dim=0, dtype=torch.int32)
            self.draft_paged_kv_last_page_len = torch.full((self.batch_size,), dec_len, dtype=torch.int32, device=self.device)
            self.prefill_wrapper.plan(
                qo_indptr=qo_indptr,
                paged_kv_indptr=self.draft_paged_kv_indptr,
                paged_kv_indices=self.draft_paged_kv_indices,
                paged_kv_last_page_len=self.draft_paged_kv_last_page_len,
                num_qo_heads=self.model.config.n_head, 
                num_kv_heads=self.model.config.n_local_heads, 
                head_dim=self.model.config.head_dim, 
                page_size=self.page_size, 
                q_data_type=self.dtype, 
                causal=True
                )
        else:
            qo_indptr = self.qo_indptr*dec_len
            self.draft_num_pages_per_request.fill_(self.draft_max_num_pages_per_request).to(torch.int32)
            self.draft_paged_kv_indices = torch.cat([torch.arange(i * self.draft_max_num_pages_per_request, i * self.draft_max_num_pages_per_request + self.draft_num_pages_per_request[i], dtype=torch.int32, device=self.device) for i in range(self.batch_size)])
            self.draft_paged_kv_indptr[1:] = torch.cumsum(self.draft_num_pages_per_request, dim=0, dtype=torch.int32)
            self.draft_paged_kv_last_page_len = torch.full((self.batch_size,), self.draft_budget%self.page_size, dtype=torch.int32, device=self.device)
            self.prefill_wrapper.plan(
                qo_indptr=qo_indptr,
                paged_kv_indptr=self.draft_paged_kv_indptr,
                paged_kv_indices=self.draft_paged_kv_indices,
                paged_kv_last_page_len=self.draft_paged_kv_last_page_len,
                num_qo_heads=self.model.config.n_head, 
                num_kv_heads=self.model.config.n_local_heads, 
                head_dim=self.model.config.head_dim, 
                page_size=self.page_size, 
                q_data_type=self.dtype, 
                causal=True
                )
        
          
    
    @torch.inference_mode()
    def clear_kv(self):
        for b in self.model.layers:
            b.attention.kv_cache.kv_cache.zero_()
            b.attention.kv_cache.draft_cache.zero_()
        self.cachelens.zero_()
        self.qo_indptr = torch.arange(self.batch_size+1, dtype=torch.int32, device=self.device)
        self.paged_kv_indptr = torch.arange(self.batch_size+1, dtype=torch.int32, device=self.device)
        self.paged_kv_indices = torch.empty(self.max_num_pages, dtype=torch.int32, device=self.device)
        self.paged_kv_last_page_len = torch.zeros((self.batch_size), dtype=torch.int32, device=self.device)
        self.num_pages_per_request = torch.zeros(self.batch_size, device=self.device, dtype=torch.int32)

        self.draft_cachelens.zero_()
        self.draft_num_pages_per_request = torch.zeros(self.batch_size, device=self.device, dtype=torch.int32)
        self.draft_paged_kv_indptr = torch.arange(self.batch_size+1, dtype=torch.int32, device=self.device)*(self.draft_budget//self.page_size + 1)
        self.draft_paged_kv_indices = torch.empty(self.draft_max_num_pages, dtype=torch.int32, device=self.device)
        self.draft_paged_kv_last_page_len = torch.zeros((self.batch_size), dtype=torch.int32, device=self.device)
        
    def clear(self):
        del self.model
        del self.cachelens
        del self.qo_indptr
        del self.paged_kv_indptr
        del self.paged_kv_indices
        del self.paged_kv_last_page_len
        del self.num_pages_per_request

        del self.draft_cachelens
        del self.draft_num_pages_per_request
        del self.draft_paged_kv_indptr
        del self.draft_paged_kv_indices
        del self.draft_paged_kv_last_page_len