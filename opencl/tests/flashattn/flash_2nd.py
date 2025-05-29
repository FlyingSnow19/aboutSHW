import argparse
import sys

import torch
import torch.nn as nn
import torch.nn.functional as F
torch.manual_seed(0)
torch.set_printoptions(linewidth=1024)

parser = argparse.ArgumentParser('')
parser.add_argument('-i', "--impl", type=int, default=0)
parser.add_argument('-b', "--batch", type=int, default=1)
parser.add_argument('-nh', "--num-heads", type=int, default=28)
parser.add_argument('-nkvh', "--num-kv-heads", type=int, default=4)
parser.add_argument('-ql', "--q-len", type=int, default=1)
parser.add_argument('-kvl', "--kv-len", type=int, default=8192)
parser.add_argument('-hs', "--head-size", type=int, default=128)
parser.add_argument('-v', "--verbose", type=int, default=-1)

#parser.add_argument('-q', "--quant_type", type=str, default="w4a", choices=['f16', 'f16b1', 'w4a', 'w4a_cpu', 'f16xmx', 'w4x'])
#parser.add_argument('-hf', '--hf_model_path', type=str, nargs='?', default='/mnt/llm_irs/models_original/Qwen2-0.5B-Instruct/')
#parser.add_argument('--save', type=str, nargs='?', default=None)
#parser.add_argument('--load', type=str, nargs='?', default=None)
args = parser.parse_args()
print(args)

enable_vprint = False
def vprint(*all_args):
    global enable_vprint
    if enable_vprint:
        print(*all_args)

batch = args.batch
q_len, q_step = args.q_len, 16
kv_len, kv_step = args.kv_len, 16
num_heads = args.num_heads
num_kv_heads = args.num_kv_heads
head_size = args.head_size
enable_gqa = num_heads > num_kv_heads

# sdpa split size, must be multiple of kv_step and kv_len should be multiple of kv_split_len
kv_split_len = 32
# dpas number for each split_len
split_block_num = kv_split_len // kv_step

# reduce step size
kv_split_data_size = 16
split_num = kv_len // kv_split_len
total_split_num = split_num * num_heads
# lse_num = batch * split_num

#q_len, q_step = 160, 16
#kv_len, kv_step = 800, 16
low = -7
high = 8
act_dtype = torch.float16
q = torch.randint(low, high, [batch, q_len, num_heads, head_size]).to(dtype=act_dtype)/high
k = torch.randint(low, high, [batch, kv_len, num_kv_heads, head_size]).to(dtype=act_dtype)/high
v = torch.randint(low, high, [batch, kv_len, num_kv_heads, head_size]).to(dtype=act_dtype)/high

# print("v.shape:", v.shape, v.dtype)
# for i in range(batch):
#     for j in range(kv_len):
#         for kk in range(num_kv_heads):
#             for h in range(head_size):
#                 k[i][j][kk][h] = (h + j*head_size)/100.0

# random attnmask
attention_mask = torch.full([batch, 1, q_len, kv_len], torch.finfo(act_dtype).min).to(dtype=act_dtype)
attention_mask[torch.rand(batch, 1, q_len, kv_len) > 0.5] = 0
#attention_mask[...] = 0

# BLHS=>BHLS
q = q.transpose(1,2)
k = k.transpose(1,2)
v = v.transpose(1,2)

#q[:,:,:,:] = q[:,:,2,:]
#attention_mask[:,:,:,:] = attention_mask[:,:,2,:]

print("q:", q.shape, q.dtype)
print("k:", k.shape, k.dtype)
print("v:", v.shape, v.dtype)
print("attention_mask:", attention_mask.shape, attention_mask.dtype)

def get_org(Q, K, V, attention_mask):
    B,H,L,S = Q.shape
    _,Hkv,_,_ = K.shape
    out = torch.zeros([B,H,L,S], dtype=Q.dtype)
    scale_factor = S**(-0.5)
    for b in range(B):
        for h in range(H):
            hkv = h // (H//Hkv)
            attn_score = Q[b, h, :, :].to(dtype=torch.float32) @ (K[b, hkv, :,:].transpose(0,1)).to(dtype=torch.float32)
            attn_score *= scale_factor
            attn_score += attention_mask[b,0,:,:]
            #print(attn_score.shape)
            attn_weights = F.softmax(attn_score, 1)
            out[b,h,:,:] = attn_weights @ V[b, hkv, :, :].to(dtype=torch.float32)
    return out

ref = F.scaled_dot_product_attention(q, k, v, attention_mask, dropout_p=0.0, enable_gqa = enable_gqa)
org = get_org(q,k,v,attention_mask)

def check_close(input, other, atol=1e-3, rtol=1e-3):
    print(f"[check_close] {input.shape}{input.dtype} vs {other.shape}{other.dtype}")
    #print("input = ", input)
    #print("other = ", other)
    rtol_max = (((input - other).abs() - 1e-5)/other.abs())[other != 0].max()
    atol_max = (((input - other).abs()) - 1e-5*other.abs()).max()
    print(f"[check_close] rtol_max: {rtol_max}")
    print(f"[check_close] atol_max: {atol_max}")
    if not torch.allclose(input, other, atol=atol, rtol=rtol):
        close_check = torch.isclose(input, other, atol=atol, rtol=rtol)
        not_close_indices = torch.where(~close_check) # Invert the close check to find failures
        print(f"Not close indices: {not_close_indices}")
        print(f"    input_tensor: {input[not_close_indices]}")
        print(f"    other_tensor: {other[not_close_indices]}")
        assert 0

check_close(ref, org, atol=1e-3, rtol=1e-2)

# [batch, seq-len, heads, size] BLHS
print("ref:", ref.shape, ref.dtype)

# blocking on kv-len dimension with online-softmax
# transpose Q@Kt to K@Qt
def get_flash0(query, key, value, attention_mask):
    global enable_vprint
    B,H,q_len,hs = query.shape
    _,Hkv,kv_len,_ = key.shape
    out = torch.zeros([B,H,q_len,hs], dtype=value.dtype)
    scale_factor = hs**(-0.5)
    for b in range(B):
        for h in range(H):
            hkv = h // (H//Hkv)
            Q = query[b, h, :, :]
            K = key[b, hkv, :, :]
            V = value[b, hkv, :, :]
            mask = attention_mask[b,0,:,:]

            # loop one time
            # q_len == 1, q_step == 1
            for i in range(0, q_len, q_step):
                i1 = min(i + q_step, q_len)
                # online softmax states:
                #     per-row max-value     : [q_step, 1]
                #     per-row sum           : [q_step, 1]
                #     current accumulated V : [q_step, S]
                #cur_max = torch.full([i1-i, 1], torch.finfo(torch.float32).min, dtype=torch.float32)
                #cur_sum = torch.full([i1-i, 1], 0, dtype=torch.float32)
                #cur_O = torch.full([i1-i, hs], 0, dtype=torch.float32)

                # we prefer transposed S since:
                #   1. per-column max/sum is easier than per-row in register, 
                #   2. loop in K direction is inner-most, so load K in noraml
                #      instead of VNNI is faster, load Q in transpose-VNNI is 
                #      done only once at loop begin.
                #   3. 
                rQt = Q[i:i1, :].transpose(0,1)  # sub Q block only transposed & VNNI packed once

                cur_O=torch.full([kv_len//kv_step, 1, hs], 0, dtype=torch.float32)
                max_comp_0=torch.full([kv_len//kv_step], 1, dtype=torch.float32)
                for j in range(0, kv_len, kv_step):
                    j1 = min(j + kv_step, kv_len)

                    if (j == args.verbose): enable_vprint = True
                    # compute in local SRAM
                    # Step8: On chip, compute S(_𝑗)𝑖= Q𝑖K𝑇 𝑗∈ R𝐵𝑟 ×𝐵𝑐.
                    rK = K[j:j1,:] #[16,128]
                    St = (rK @ rQt).to(dtype=torch.float32) #[16,1]
                    MaskT = mask[i:i1, j:j1].transpose(0,1) #[16,1]

                    vprint("rK=", rK.shape)
                    vprint("rQt=",rQt.shape)
                    vprint("St=",St.shape)
                    vprint("MaskT=",MaskT.shape)

                    St *= scale_factor
                    St += mask[i:i1, j:j1].transpose(0,1)
                    vprint("St=",St.shape)

                    rowmax = St.max(0, keepdim=True).values # [1,1]
                    if j == 0:
                        cur_max = rowmax
                    else:
                        rowmax = torch.maximum(cur_max, rowmax)
                    vprint("rowmax=", rowmax.shape)

                    # compute in local SRAM
                    St = torch.exp(St - rowmax) # [16,1]
                    vprint("St(Pt)=", St.shape)

                    rowsumP = St.sum(0, keepdim=True) # [1,1]
                    vprint("rowsumP=", rowsumP.shape)

                    # corrected sum of previous block
                    if j > 0:
                        max_comp = torch.exp(cur_max - rowmax)
                        max_comp_0[j//kv_step] = max_comp

                    if j == 0:
                        cur_sum = rowsumP
                    else:
                        cur_sum = cur_sum * max_comp + rowsumP

                    # softmax normalize is saved accoridng to flash-attn2 section 3.1.1
                    # We can instead maintain an “un-scaled” version of O(2) and keep around the statistics ℓ(2)
                    partial_attn_weight = St.to(dtype=torch.float16).transpose(0,1) # [1,16]
                    
                    vprint("P=", partial_attn_weight.shape)

                    rV = V[j:j1, :] # [16,128]
                    vprint("rV=",rV.shape)

                    # correct last Output to current statistics
                    cur_O[j//kv_step,:,:] = partial_attn_weight @ rV # [:,1,128]
                    vprint("cur_O2=", cur_O.shape)

                    cur_max = rowmax
                    if (j == args.verbose): assert 0

                cur_O_f32 = cur_O[0,:,:]
                for j in range(1, kv_len//kv_step):
                    cur_O_f32 = cur_O_f32 *  max_comp_0[j] + cur_O[j,:,:]
                vprint("cur_O_f32=", cur_O_f32.shape)
                cur_O_f16 = (cur_O_f32/cur_sum.transpose(0, 1)).to(torch.float16)

                if (i == args.verbose):
                    enable_vprint = True
                    vprint("cur_O_f16=", cur_O_f16.shape, cur_O_f16)
                    assert 0

                out[b, h, i:i1, :] = cur_O_f16
    return out

# blocking on kv-len dimension with online-softmax
# Softmax(Q@Kt)@V
def get_flash1(query, key, value, attention_mask):
    global enable_vprint
    B,H,q_len,hs = query.shape
    _,Hkv,kv_len,_ = key.shape
    out = torch.zeros([B,H,q_len,hs], dtype=value.dtype)
    scale_factor = hs**(-0.5)
    for b in range(B):
        for h in range(H):
            hkv = h // (H//Hkv)
            Q = query[b, h, :, :]
            K = key[b, hkv, :, :]
            V = value[b, hkv, :, :]
            mask = attention_mask[b,0,:,:]

            # loop one time
            # q_len == 1, q_step == 1
            for i in range(0, 1, 1):
                i1 = 1
                # online softmax states:
                #     per-row max-value     : [1, 1]
                #     per-row sum           : [1, 1]
                #     current accumulated V : [1, S]
                #cur_max = torch.full([i1-i, 1], torch.finfo(torch.float32).min, dtype=torch.float32)
                #cur_sum = torch.full([i1-i, 1], 0, dtype=torch.float32)
                #cur_O = torch.full([i1-i, hs], 0, dtype=torch.float32)

                rQ = Q[0, :].reshape(1,hs) # [1,128] sub Q block VNNI packed

                cur_O=torch.full([kv_len//kv_step, 1, hs], 0, dtype=torch.float32)
                max_comp_0=torch.full([kv_len//kv_step], 1, dtype=torch.float32)

                for j in range(0, kv_len, kv_step):
                    j1 = min(j + kv_step, kv_len)

                    if (j == args.verbose): enable_vprint = True
                    # compute in local SRAM
                    # Step8: On chip, compute S(_𝑗)𝑖= Q𝑖K𝑇 𝑗∈ R𝐵𝑟 ×𝐵𝑐.
                    rKt = K[j:j1,:].transpose(0,1) #[16,128]->[128,16]
                    rS = (rQ @ rKt).to(dtype=torch.float32).reshape(1,kv_step) #[1,16]
                    rMask = mask[i:i1, j:j1] #[1,16]

                    vprint("rK=", rKt.shape)
                    vprint("rQt=",rQ.shape)
                    vprint("rS=",rS.shape)
                    vprint("rMask=",rMask.shape)

                    rS *= scale_factor
                    rS += rMask
                    vprint("rS=",rS.shape)

                    rowmax = rS.max(1, keepdim=True).values # [1,1]
                    if j == 0:
                        cur_max = rowmax
                    else:
                        rowmax = torch.maximum(cur_max, rowmax)
                    vprint("rowmax=", rowmax.shape)

                    # compute in local SRAM
                    rS = torch.exp(rS - rowmax) # [1,16]
                    vprint("St(Pt)=", rS.shape)

                    rowsumP = rS.sum(1, keepdim=True) # [1,1]
                    vprint("rowsumP=", rowsumP.shape)

                    # corrected sum of previous block
                    if j > 0:
                        max_comp = torch.exp(cur_max - rowmax)
                        vprint("max_comp=", max_comp.shape)
                        max_comp_0[j//kv_step] = max_comp

                    if j == 0:
                        cur_sum = rowsumP
                    else:
                        cur_sum = cur_sum * max_comp + rowsumP

                    # softmax normalize is saved accoridng to flash-attn2 section 3.1.1
                    # We can instead maintain an “un-scaled” version of O(2) and keep around the statistics ℓ(2)
                    partial_attn_weight = rS.to(dtype=torch.float16) # [1,16]
                    
                    vprint("P=", partial_attn_weight.shape)

                    rV = V[j:j1, :] # [16,128]
                    vprint("rV=",rV.shape)

                    # correct last Output to current statistics
                    cur_O[j//kv_step,:,:] = partial_attn_weight @ rV # [:,1,128]
                    vprint("cur_O2=", cur_O.shape)

                    cur_max = rowmax
                    if (j == args.verbose): assert 0

                cur_O_f32 = cur_O[0,:,:]
                for j in range(1, kv_len//kv_step):
                    cur_O_f32 = cur_O_f32 *  max_comp_0[j] + cur_O[j,:,:]
                vprint("cur_O_f32=", cur_O_f32.shape)
                vprint("cur_sum=", cur_sum.shape)
                cur_O_f16 = (cur_O_f32/cur_sum).to(torch.float16)

                if (i == args.verbose):
                    enable_vprint = True
                    print("cur_O_f16=", cur_O_f16.shape, cur_O_f16)
                    assert 0

                out[b, h, i:i1, :] = cur_O_f16
    return out

# Split KV online-softmax
def get_flash2(query, key, value, attention_mask):
    global enable_vprint
    B,H,q_len,hs = query.shape
    _,Hkv,kv_len,_ = key.shape
    out = torch.zeros([B,H,q_len,hs], dtype=value.dtype)
    scale_factor = hs**(-0.5)
    for b in range(B):
        for h in range(H):
            hkv = h // (H//Hkv)
            Q = query[b, h, :, :]
            K = key[b, hkv, :, :]
            V = value[b, hkv, :, :]
            mask = attention_mask[b,0,:,:]

            # loop kv_split
            cur_O=torch.full([kv_len//kv_split_len, 1, hs], 0, dtype=torch.float32)
            cur_O_f32=torch.full([1, hs], 0, dtype=torch.float32)
            cur_O_f16=torch.full([1, hs], 0, dtype=torch.float16)
            lse=torch.full([kv_len//kv_split_len], 0, dtype=torch.float32)
            for i in range(0, kv_len, kv_split_len):
                i1 = min(i + kv_split_len, kv_len)
                # online softmax states:
                #     per-row max-value     : [1, 1]
                #     per-row sum           : [1, 1]
                #     current accumulated V : [1, S]
                #cur_max = torch.full([i1-i, 1], torch.finfo(torch.float32).min, dtype=torch.float32)
                #cur_sum = torch.full([i1-i, 1], 0, dtype=torch.float32)
                #cur_O = torch.full([i1-i, hs], 0, dtype=torch.float32)
 
                rQ = Q[0, :].reshape(1,hs) # [1,128] sub Q block VNNI packed
                vprint("rQ = ", rQ)
                vprint("mask = ", mask)

                cur_lse = 0.0
                cur_sum = 0.0
                for j in range(i, i1, kv_step):
                    j1 = min(j + kv_step, kv_len)

                    if (j == args.verbose): enable_vprint = True
                    # compute in local SRAM
                    # Step8: On chip, compute S(_𝑗)𝑖= Q𝑖K𝑇 𝑗∈ R𝐵𝑟 ×𝐵𝑐.
                    rKt = K[j:j1,:].transpose(0,1) #[16,128]->[128,16]
                    rS = (rQ @ rKt).to(dtype=torch.float32).reshape(1,kv_step) #[1,16]
                    rMask = mask[0:1, j:j1] #[1,16]

                    vprint("rK=", rKt)
                    vprint("rQt=",rQ)
                    vprint("rS=",rS)
                    vprint("rMask=",rMask)

                    rS *= scale_factor
                    rS += rMask
                    vprint("rS=",rS)

                    cur_lse += torch.exp(rS).sum(1, keepdim=True).item() # [1,1]
                    vprint("rS=", rS)
                    vprint("exp(rS)=", torch.exp(rS))
                    vprint("cur_lse=", cur_lse)

                    rowmax = rS.max(1, keepdim=True).values # [1,1]
                    if j == 0:
                        cur_max = rowmax
                    else:
                        rowmax = torch.maximum(cur_max, rowmax)
                    vprint("rowmax=", rowmax.shape)

                    # compute in local SRAM
                    rS = torch.exp(rS - rowmax) # [1,16]
                    vprint("St(Pt)=", rS)

                    rowsumP = rS.sum(1, keepdim=True) # [1,1]
                    vprint("rowsumP=", rowsumP)

                    # corrected sum of previous block
                    if j > 0:
                        max_comp = torch.exp(cur_max - rowmax)
                        vprint("max_comp=", max_comp.shape)

                    if j == 0:
                        cur_sum = rowsumP
                    else:
                        cur_sum = cur_sum * max_comp + rowsumP

                    # softmax normalize is saved accoridng to flash-attn2 section 3.1.1
                    # We can instead maintain an “un-scaled” version of O(2) and keep around the statistics ℓ(2)
                    partial_attn_weight = rS.to(dtype=torch.float16) # [1,16]
                    
                    vprint("P=", partial_attn_weight)

                    rV = V[j:j1, :] # [16,128]
                    vprint("rV=",rV)

                    # correct last Output to current statistics
                    if j== 0:
                        cur_O[i//kv_split_len,:,:] = partial_attn_weight @ rV
                    else:
                        cur_O[i//kv_split_len,:,:] = cur_O[i//kv_split_len,:,:] * max_comp;
                        cur_O[i//kv_split_len,:,:] += partial_attn_weight @ rV # [:,1,128]
                    vprint("j = ", j)
                    vprint("cur_O2=",  cur_O[i//kv_split_len,:,:])

                    cur_max = rowmax
                    if (j == args.verbose): assert 0

                lse[i//kv_split_len] = cur_lse
                cur_O[i//kv_split_len,:,:] = cur_O[i//kv_split_len,:,:] / cur_sum
                vprint("cur_sum=", cur_sum.shape)
                vprint("cur_O=",  cur_O[i//kv_split_len,:,:])

            # reduce
            for i in range(0, kv_len//kv_split_len, 1):
                for j in range(0, hs, kv_step):
                    stop = min(j + kv_step, hs)
                    print("i=", i, ", j = ", j, ": cur_O[i,:,:]=", cur_O[i,0,j:stop])
            vprint("lse=", lse)
            # print("lse=", lse.shape) # [4]
            sum_lse = lse.sum(0)
            # print("cur_O=", cur_O.shape) # 
            # print("cur_O_f32=", cur_O_f32.shape) #
            vprint("lse = ", lse)
            vprint("sum_lse = ", sum_lse)
            for i in range(0, kv_len//kv_split_len, 1):
                cur_O_f32 += cur_O[i,:,:] * lse[i] / sum_lse
            cur_O_f16 = cur_O_f32.to(torch.float16)
            out[b, h, :, :] = cur_O_f16
            # print("cur_O_f16=", cur_O_f16.shape) #
            # print("out=", out.shape) #
    #print("out = ", out[0,0,0,:])
    print("out = ", out)
    return out

if args.impl == 0:
    f0 = get_flash2(q,k,v,attention_mask)
    check_close(org, f0, atol=1e-2, rtol=1e-3)
    print("=========== PASS ===========")
    sys.exit(0)


print()
print("GPU cm kernels for flash attn2:")

#====================================================================================================
# using the same parameter & inputs, develop cm kernels which produces the same output
# prototyping CM kernels
from clops import cl
import numpy as np
import time

# transpose back to orginal shape: [batch, q_len, num_heads, head_size]
q = q.transpose(1,2)
k = k.transpose(1,2)
v = v.transpose(1,2)
print("q:", q.shape, q.dtype)
print("k:", k.shape, k.dtype)
print("v:", v.shape, v.dtype)
print("attention_mask:", attention_mask.shape, attention_mask.dtype)

def pyeval(src):
    result_src = ""
    for line in src.splitlines():
        if line.startswith("#pyeval"):
            new_line = eval(line[8:])
            result_src += new_line + "\n"
            # print(f"[pyeval] {new_line}")
        else:
            result_src += line + "\n"
    return result_src

'''
ugemm_qk: [q_step, head_size] x [head_size, kv_step]
ugemm_kq: [kv_step, head_size] x [head_size, q_step]
ugemm_pv: [q_step, kv_step] x [kv_step, head_size]
'''

scale_factor = 1.0/(head_size**0.5)

GWS=[batch, num_heads, kv_len//kv_split_len] #[batch, num_heads, 1024//16=64]
WG_SIZE = max(kv_split_len//kv_step, 1) # 32//16= 2
LWS=[1, 1, WG_SIZE]

print("GWS=", GWS)
print("LWS=", LWS)

#========================================================================
# Optimization Log
r'''
increase WG_SIZE to 16 (-70ms)
use GRF to store temp Output(rO) instead of SLM, requires `-Qxcm_register_file_size=256` (-40ms)
avoid type-promotion:   St = cm_mul<float>(St, scale_factor);    =>   St = cm_mul<float>(St, (float)scale_factor);   (-10ms) 
change dtype of attention_mask from float to half (-14ms)
avoid indirect register access: unroll for loop which access matrix rows using loop-index
use GRF to store temp Input rQ instead of SLM, this allows more Work-Groups to be packed into same Xe-core!!!
'''
#========================================================================

src1 = r'''
//# CM kernel for flash attn, reference

#pyeval f"#define num_heads {num_heads}"
#pyeval f"#define num_kv_heads {num_kv_heads}"
#pyeval f"#define head_size {head_size}"
#pyeval f"#define q_step {q_step}"
#pyeval f"#define kv_step {kv_step}"
#pyeval f"#define scale_factor {scale_factor}"
#pyeval f"#define args_verbose {args.verbose}"
#pyeval f"#define WG_SIZE {WG_SIZE}"
#pyeval f"#define kv_split_len {kv_split_len}"
#pyeval f"#define kv_split_data_size {kv_split_data_size}"

#pyeval f"#define split_num {split_num}"
#pyeval f"#define split_block_num {split_block_num}"

#pyeval f"#define total_split_num {total_split_num}"

#define SystolicDepth 8
#define RepeatCount 1
#define VNNI_WIDTH 2
#define REG_K (SystolicDepth * VNNI_WIDTH)
#define REG_M RepeatCount
#define REG_N 16

#define USE_LSC_BLOCK_2D_DESC 0

#define PRINT_THR_ID 1000
#define PRINT_HEAD_ID 1000

// static_assert(q_step == 16);
static_assert(kv_step == 16);

template<typename T, int M, int N>
void show(const matrix<T, M, N> mat) {
    for(int m = 0; m < M; m ++) {
        printf("\t[");
        for(int n = 0; n < N; n ++) {
            printf("%8.4f,", mat[m][n]);
        }
        printf("],\n");
    }
    printf("]\n");
}

CM_INLINE uint64_t get_clock() {
    auto clk = cm_clock();
    return ((uint64_t)clk[1]) << 32 | clk[0];
}

template <typename T1, typename T2>
CM_INLINE void Transpose_16x16(matrix_ref<T1, 16, 16> in,
                               matrix_ref<T2, 16, 16> out) {
  matrix<T2, 16, 16> bBuf;
  bBuf.row(0) = in.template select<4, 1, 4, 4>(0, 0);   // 0,4,8,c
  bBuf.row(1) = in.template select<4, 1, 4, 4>(4, 0);   // 0,4,8,c
  bBuf.row(2) = in.template select<4, 1, 4, 4>(8, 0);   // 0,4,8,c
  bBuf.row(3) = in.template select<4, 1, 4, 4>(12, 0);  // 0,4,8,c
  bBuf.row(4) = in.template select<4, 1, 4, 4>(0, 1);   // 1,5,9,d
  bBuf.row(5) = in.template select<4, 1, 4, 4>(4, 1);   // 1,5,9,d
  bBuf.row(6) = in.template select<4, 1, 4, 4>(8, 1);   // 1,5,9,d
  bBuf.row(7) = in.template select<4, 1, 4, 4>(12, 1);  // 1,5,9,d
  bBuf.row(8) = in.template select<4, 1, 4, 4>(0, 2);   // 2,6,a,e
  bBuf.row(9) = in.template select<4, 1, 4, 4>(4, 2);   // 2,6,a,e
  bBuf.row(10) = in.template select<4, 1, 4, 4>(8, 2);  // 2,6,a,e
  bBuf.row(11) = in.template select<4, 1, 4, 4>(12, 2); // 2,6,a,e
  bBuf.row(12) = in.template select<4, 1, 4, 4>(0, 3);  // 3,7,b,f
  bBuf.row(13) = in.template select<4, 1, 4, 4>(4, 3);  // 3,7,b,f
  bBuf.row(14) = in.template select<4, 1, 4, 4>(8, 3);  // 3,7,b,f
  bBuf.row(15) = in.template select<4, 1, 4, 4>(12, 3); // 3,7,b,f

  out.row(0) = bBuf.template select<4, 1, 4, 4>(0, 0);   // 0
  out.row(1) = bBuf.template select<4, 1, 4, 4>(4, 0);   // 1
  out.row(2) = bBuf.template select<4, 1, 4, 4>(8, 0);   // 2
  out.row(3) = bBuf.template select<4, 1, 4, 4>(12, 0);  // 3
  out.row(4) = bBuf.template select<4, 1, 4, 4>(0, 1);   // 4
  out.row(5) = bBuf.template select<4, 1, 4, 4>(4, 1);   // 5
  out.row(6) = bBuf.template select<4, 1, 4, 4>(8, 1);   // 6
  out.row(7) = bBuf.template select<4, 1, 4, 4>(12, 1);  // 7
  out.row(8) = bBuf.template select<4, 1, 4, 4>(0, 2);   // 8
  out.row(9) = bBuf.template select<4, 1, 4, 4>(4, 2);   // 9
  out.row(10) = bBuf.template select<4, 1, 4, 4>(8, 2);  // a
  out.row(11) = bBuf.template select<4, 1, 4, 4>(12, 2); // b
  out.row(12) = bBuf.template select<4, 1, 4, 4>(0, 3);  // c
  out.row(13) = bBuf.template select<4, 1, 4, 4>(4, 3);  // d
  out.row(14) = bBuf.template select<4, 1, 4, 4>(8, 3);  // e
  out.row(15) = bBuf.template select<4, 1, 4, 4>(12, 3); // f
}

template <typename T1, typename T2>
CM_INLINE void Transpose_8x8(matrix_ref<T1, 8, 8> in, matrix_ref<T2, 8, 8> out) {
  matrix<T2, 8, 8> temp;
  temp.row(0) = in.template select<2, 1, 4, 2>(0, 0);
  temp.row(1) = in.template select<2, 1, 4, 2>(2, 0);
  temp.row(2) = in.template select<2, 1, 4, 2>(4, 0);
  temp.row(3) = in.template select<2, 1, 4, 2>(6, 0);
  temp.row(4) = in.template select<2, 1, 4, 2>(0, 1);
  temp.row(5) = in.template select<2, 1, 4, 2>(2, 1);
  temp.row(6) = in.template select<2, 1, 4, 2>(4, 1);
  temp.row(7) = in.template select<2, 1, 4, 2>(6, 1);

  out.row(0) = temp.template select<4, 1, 2, 4>(0, 0);
  out.row(2) = temp.template select<4, 1, 2, 4>(0, 1);
  out.row(4) = temp.template select<4, 1, 2, 4>(0, 2);
  out.row(6) = temp.template select<4, 1, 2, 4>(0, 3);
  out.row(1) = temp.template select<4, 1, 2, 4>(4, 0);
  out.row(3) = temp.template select<4, 1, 2, 4>(4, 1);
  out.row(5) = temp.template select<4, 1, 2, 4>(4, 2);
  out.row(7) = temp.template select<4, 1, 2, 4>(4, 3);
}

extern "C" _GENX_MAIN_ void cm_sdpa_2nd(
    int q_len,// 1
    int kv_len,
    half* query [[type("svmptr_t")]],
    half* key [[type("svmptr_t")]],
    half* value [[type("svmptr_t")]],
    half* output [[type("svmptr_t")]],
    half* mask [[type("svmptr_t")]],
    float* lse [[type("svmptr_t")]]
    ) {
    //# query [batch, 1,      num_heads, S]
    //#   key [batch, kv_len, num_heads, S]
    //# value [batch, kv_len, num_heads, S]
    //# output[batch, 1,      num_heads, S]

    auto batch = cm_global_id(0);
    auto h = cm_global_id(1);
    auto hkv = h / (num_heads/num_kv_heads);
    auto wg_local_id = cm_local_id(2);

    //# kv_split_len --> EU thread
    // auto wg_thread_id = (cm_group_id(2) * WG_SIZE + wg_local_id);
    auto wg_thread_id = cm_global_id(2);
    auto o_start = wg_thread_id * head_size;

    uint kv_pitch = num_kv_heads * head_size * sizeof(half);
    uint qo_pitch = num_heads * head_size * sizeof(half);

#if USE_LSC_BLOCK_2D_DESC
    //# vector load cannot be used for block_2d_desc
    //# note: candidate template ignored: deduced type 'details::Block2DRefTy<half, 1U, 16U, 1U, (LoadOp)0U>' (aka 'vector_ref<half,32>') of 1st parameter
    //# b2dK reinterpret as 32bit(DWORD) for transposed load(combined with VNNI)
    lsc::block_2d_desc<uint, 1, REG_N, REG_K/2> b2dK(reinterpret_cast<uint*>(key + (batch*num_kv_heads*kv_len + hkv)*head_size),   kv_len - 1, head_size*sizeof(half) - 1, kv_pitch - 1, 0, 0);
    lsc::block_2d_desc<half, 1, REG_K, REG_N> b2dV(value + (batch*num_kv_heads*kv_len + hkv)*head_size, kv_len - 1, head_size*sizeof(half) - 1, kv_pitch - 1, 0, 0);
#else
    uint kv_offset = (batch*num_kv_heads*kv_len + hkv)*head_size;
    uint kv_stride = num_kv_heads * head_size;
    uint kv_x0 = 0, kv_y0 = 0;
    uint kv_x1 = head_size*sizeof(half);
    uint kv_y1 = kv_len;
#endif

    //# Load Q into register(as dpas-A tile)
    matrix <half, head_size/REG_K, REG_M*REG_K> Qmat;
    uint qo_offset = (batch*num_heads*q_len + h)*head_size;
    for(int k = 0, ri = 0; k < head_size; k += REG_K, ri++) {
        cm_svm_block_read<half, REG_M * REG_K>((svmptr_t)(query + qo_offset + k), Qmat[ri].format<half>());
    }

    //if(wg_thread_id==PRINT_THR_ID && h == PRINT_HEAD_ID) {
    //    printf("Qmat loaded, wg_thread_id=%d\n", wg_thread_id);
    //    show(Qmat);
    //}

    //# rS = Q @ Kt
    //#  split_block_num * [REG_M, REG_K] * [REG_K, REG_N] = split_block_num * [REG_M, REG_N]
    matrix<float, REG_M * split_block_num, REG_N> rS = 0;
    for(int kv_pos = 0, ki = 0; kv_pos < kv_split_len; kv_pos += kv_step, ki++) {
        auto rSvec = rS[ki].format<float>();
        uint kv_offset_y = wg_thread_id * kv_split_len + kv_pos;

        #pragma unroll
        for(int k = 0, ri = 0; k < head_size/2; k += REG_K/2, ri ++ ) {
            matrix<half, REG_K, REG_N> Kt;
        #if USE_LSC_BLOCK_2D_DESC
            //# Load Kt into register & pack as VNNI(as dpas-B tile)
            //# DWORD transposed load == (transposed + VNNI) load
            b2dK.set_block_x(k);
            cm_load<lsc::Transpose>(Kt.format<uint>(), b2dK.set_block_y(kv_offset_y));
        #else
            matrix<uint, REG_N, REG_K/2> temp;
            uint cur_kv_offset = kv_offset + kv_offset_y * kv_stride + k * 2;// uint --> half
            #pragma unroll
            for(int kk = 0; kk < REG_N; kk++) {
                cm_svm_block_read<uint, REG_K/2>((svmptr_t)(key + cur_kv_offset + kk * kv_stride), temp[kk].format<uint>());
            }
            Transpose_8x8(temp.select<8,1,8,1>(0,0), Kt.format<uint, REG_K/2, REG_N>().select<8,1,8,1>(0,0));
            Transpose_8x8(temp.select<8,1,8,1>(8,0), Kt.format<uint, REG_K/2, REG_N>().select<8,1,8,1>(0,8));
        #endif

            rSvec = cm_dpas<CM_PRECISION_HF, CM_PRECISION_HF, SystolicDepth, RepeatCount>(
                        rSvec,
                        Kt.format<int32_t>(),
                        Qmat[ri].format<int32_t>());
        }
    }

    // online softmax
    float cur_sum = 0.0f;
    float cur_lse = 0.0f;
    matrix<half, split_block_num * REG_M, REG_K> Pmat = 0;
    {
        //# Load Mask into register
        matrix<half, REG_M * split_block_num, REG_N> MaskMat;
        uint mask_offset = batch * q_len * kv_len + wg_thread_id * kv_split_len;
        cm_svm_block_read<half, REG_M * split_block_num * REG_N>((svmptr_t)(mask + mask_offset), MaskMat.format<half>());

        rS = cm_mul<float>(rS, (float)scale_factor);  // convert scale_factor into (float), or it will be promoted to double
        rS = cm_add<float>(rS, MaskMat);

        // compute lse
        constexpr float log2e = 1.4426950408889634f;
        vector<float, split_block_num * REG_N> rS_exp = cm_exp(rS.format<float>()*log2e);
        cur_lse += cm_sum<float>(rS_exp);

        // compute row_max
        auto rSv = rS.format<float>();
        float row_max = rSv[0];
        for(int r = 1; r < rSv.n_elems(); r++)
            row_max = cm_max<float>(row_max, rSv[r]);

        // compute P = exp(rS - row_max)
        Pmat= cm_exp((rS - row_max)*log2e);

        // compute row sum of P
        auto rPv = Pmat.format<half, 1, split_block_num * REG_N>();
        cur_sum = cm_sum<float>(rPv[0]);
    }

    //# rO = P * V
    matrix <float, head_size/REG_K, REG_M*REG_N> Omat = 0;
    for(int kv_pos = 0, ki = 0; kv_pos < kv_split_len; kv_pos += kv_step, ki++) {
        uint kv_offset_y = wg_thread_id * kv_split_len + kv_pos;
        #pragma unroll
        for(int k = 0, ri = 0; k < head_size; k += REG_K, ri ++ ) {
            // Load V into register & pack as VNNI(as dpas-B tile)
            matrix<half, REG_M, REG_K*REG_N> Vmat;
        #if USE_LSC_BLOCK_2D_DESC
            b2dV.set_block_x(k);
            cm_load<lsc::VNNI>(Vmat[0].format<half>(), b2dV.set_block_y(kv_offset_y));
        #else
            matrix<half, REG_K, REG_N> temp;
            uint cur_kv_offset = kv_offset + kv_offset_y * kv_stride + k;
            #pragma unroll
            for(int kk = 0; kk < REG_K; kk++) {
                cm_svm_block_read<half, REG_N>((svmptr_t)(value + cur_kv_offset + kk * kv_stride), temp[kk].format<half>());
            }
            auto Vref = Vmat[0].format<half, REG_K/2, 2*REG_N>();
            Vref.select<REG_K/2, 1, REG_N, 2>(0, 0) = temp.select<REG_K/2, 2, REG_N, 1>(0, 0);
            Vref.select<REG_K/2, 1, REG_N, 2>(0, 1) = temp.select<REG_K/2, 2, REG_N, 1>(1, 0);
        #endif
            //if(wg_thread_id==0){
            //    show(Vmat[0].format<half, REG_K, REG_N>());
            //}
            Omat[ri] = cm_dpas<CM_PRECISION_HF, CM_PRECISION_HF, SystolicDepth, RepeatCount>(
                        Omat[ri],
                        Vmat[0].format<int32_t>(),
                        Pmat[ki].format<int32_t>());
        }
    }
    
    //# save Output
    matrix<half, REG_M, REG_N> cur_O_f16;
    uint o_offset = batch * split_num * num_heads * head_size + split_num * h * head_size + wg_thread_id * head_size;
    #pragma unroll
    for(int k = 0, ri=0; k < head_size; k += REG_K, ri++) {
        auto cO = Omat[ri].format<float, REG_M, REG_N>();
        cur_O_f16= cm_div_ieee(cO, cur_sum);
        cm_svm_block_write<half, REG_N>((svmptr_t)(output + o_offset + k),cur_O_f16.format<half>());
    }
    uint lse_offset = batch * num_heads * split_num + h * split_num + wg_thread_id;
    lse[lse_offset] = cur_lse;
}

extern "C" _GENX_MAIN_ void cm_sdpa_2nd_reduce(
    int kv_len,
    half* input [[type("svmptr_t")]], // 
    half* output [[type("svmptr_t")]],
    float* lse [[type("svmptr_t")]]
    ) {
        auto batch = cm_global_id(0);
        auto head = cm_global_id(1);
        auto offset = cm_group_id(2) * kv_split_data_size;

        // load lse
        uint lse_offset = batch * total_split_num + head * split_num;
        vector<float, split_num> lse_vec;
        cm_svm_block_read<float, split_num>((svmptr_t)(lse + lse_offset), lse_vec.format<float>());
        float total_lse = cm_sum<float>(lse_vec);

        // load input, total_split_num = head_nums * split_num;
        matrix<half, 1, kv_split_data_size> out_mat = 0;
        matrix<half, 1, kv_split_data_size> data_mat;
        uint input_offset = batch * total_split_num * head_size + head * split_num * head_size + offset;
        #pragma unroll
        for(int k = 0; k < split_num; k ++) {
            cm_svm_block_read<half, kv_split_data_size>((svmptr_t)(input + input_offset), data_mat.format<half>());
            input_offset += head_size;
            out_mat += cm_mul<half>(data_mat, (float)(lse_vec[k]/total_lse));
        }

        // write output
        uint output_offset = batch * num_heads * head_size + head * head_size + offset;
        cm_svm_block_write<half, kv_split_data_size>((svmptr_t)(output + output_offset),out_mat.format<half>());
    }
'''

cl.profiling(True)

t_q = cl.tensor(q.detach().numpy())
t_k = cl.tensor(k.detach().numpy())
t_v = cl.tensor(v.detach().numpy())
t_out = cl.tensor([batch, num_heads, split_num, head_size], np.dtype(np.float16))
t_out_final = cl.tensor([batch, 1, num_heads, head_size], np.dtype(np.float16))
t_mask = cl.tensor(attention_mask.detach().numpy())
t_lse = cl.tensor([batch, num_heads, split_num], np.dtype(np.float32))

# f"-cmc -mdump_asm -g2 "
print("compiling ...")
cm_kernels = cl.kernels(pyeval(src1), f"-cmc -Qxcm_register_file_size=256 -mdump_asm -g2")
print("first call ...")
cm_kernels.enqueue("cm_sdpa_2nd", GWS, LWS, q_len, kv_len, t_q, t_k, t_v, t_out, t_mask, t_lse)

f0 = torch.from_numpy(t_out.numpy())
print("f0 = ", f0.shape, f0.dtype)
# for i in range(num_heads):
#     for j in range(split_num):
#         for k in range(0, head_size, kv_step):
#             stop = min(k + kv_step, head_size)
#             print(f"f0[{i}, {j}, {k}:{stop}] = ", f0[0, i, j, k:stop])

lse0 = torch.from_numpy(t_lse.numpy())
# print("lse0 = ", lse0.shape, lse0.dtype, lse0)
print("lse_sum = ", lse0.sum(2))

GWS = [batch, num_heads, head_size//kv_split_data_size]
LWS = [1, 1, 1]
print("GWS=", GWS)
print("LWS=", LWS)
cm_kernels.enqueue("cm_sdpa_2nd_reduce", GWS, LWS, kv_len, t_out, t_out_final, t_lse)
f1 = torch.from_numpy(t_out_final.numpy())

#print("f1 = ", f1[0,0,0,:])
#print("f1 = ", f1)
#sys.exit(0)

loop_cnt = 100
all_layers = []
mem_size = 0
while len(all_layers) < loop_cnt and mem_size < 8e9:
    all_layers.append([
        cl.tensor(q.detach().numpy()),
        cl.tensor(k.detach().numpy()),
        cl.tensor(v.detach().numpy()),
        cl.tensor([batch, num_heads, split_num, head_size], np.dtype(np.float16)),
        cl.tensor([batch, 1, num_heads, head_size], np.dtype(np.float16)),
    ])
    mem_size += q.numel() * q.element_size()
    mem_size += k.numel() * k.element_size()
    mem_size += v.numel() * v.element_size()
    print(f"nlayers={len(all_layers)} mem_size={mem_size*1e-9:.3f} GB")

for i in range(loop_cnt):
    j  = i % len(all_layers)
    cm_kernels.enqueue("cm_sdpa_2nd", GWS, LWS, q_len, kv_len,
                       all_layers[j][0],
                       all_layers[j][1],
                       all_layers[j][2],
                       all_layers[j][3],
                       t_mask, t_lse)
    cm_kernels.enqueue("cm_sdpa_2nd_reduce", GWS, LWS, kv_len, all_layers[j][3],all_layers[j][4], t_lse)

latency = cl.finish()
for ns in latency:
    print(f"  {ns*1e-6:.3f} ms")

check_close(org.transpose(1,2), f1, atol=1e-2, rtol=1e-3)
print(f"=========== cm_sdpa_2nd PASS GWS={GWS} LWS={LWS}  ===========")

sys.exit(0)
