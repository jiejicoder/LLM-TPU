import os
import sys
import json
import time
import random
import argparse
import numpy as np
from transformers import AutoTokenizer

import chat

from test_a16matmul import cosine_similarity

class Qwen:
    def __init__(self, args):
        # preprocess parameters, such as prompt & tokenizer
        # devid
        self.devices = [int(d) for d in args.devid.split(",")]
        self.model_path = args.model_path

        # other parameters
        self.seq_length_list = [10240,8192,7168,6144,5120,4096,3072,2048,1024]
        self.prefill_length_list = [8320,8192,7168,6144,5120,4096,3072,2048,1024]
        self.lora_path = args.lora_path

        # load tokenizer
        print("Load " + args.tokenizer_path + " ...")
        self.tokenizer = AutoTokenizer.from_pretrained(
            args.tokenizer_path, trust_remote_code=True
        )

        # warm up
        self.tokenizer.decode([0])
        self.EOS = self.tokenizer.eos_token_id

        self.model = chat.Qwen()
        self.init_params(args)

    def load_model(self, model_path, read_bmodel):
        load_start = time.time()
        self.model.init(self.devices, model_path, read_bmodel) # when read_bmodel = false, not to load weight, reuse weight
        load_end = time.time()
        print(f"\nLoad Time: {(load_end - load_start):.3f} s")

    def update_lora_and_lora_embedding(self, lora_path):
        if not os.path.exists(lora_path):
            raise FileNotFoundError(f"{lora_path} not found")

        net_idx = ",".join(str(2 * i) for i in range(28)) + ",56" # lora + lora_embedding
        mem_idx = ",".join(str(i) for i in range(28)) + ",28"
        weight_idx = ["1,2,3,4,5,6,9,10,13,14,16,17,19,20"]*28 + ["0,1"]

        start_time = time.time()
        self.model.update_bmodel_weight(self.model_path, lora_path, net_idx, mem_idx, weight_idx)
        end_time = time.time()
        print(f"\nLora Update Time: {(end_time - start_time):.3f} s")

    def empty_lora(self):
        net_idx = ",".join(str(2 * i) for i in range(28)) # lora
        mem_idx = ",".join(str(i) for i in range(28))
        weight_idx = ["1,2,3,4,5,6,9,10,13,14,16,17,19,20"]*28

        start_time = time.time()
        self.model.empty_bmodel_weight(self.model_path, net_idx, mem_idx, weight_idx)
        end_time = time.time()
        print(f"\nLora Empty Time: {(end_time - start_time):.3f} s")

    def empty_lora_embedding(self):
        net_idx = "56" # lora_embedding
        mem_idx = "28"
        weight_idx =  ["0,1"]

        start_time = time.time()
        self.model.empty_bmodel_weight(self.model_path, net_idx, mem_idx, weight_idx)
        end_time = time.time()
        print(f"\nLora Empty Time: {(end_time - start_time):.3f} s")

    def empty_lora_and_lora_embedding(self):
        net_idx = ",".join(str(2 * i) for i in range(28)) + ",56" # lora + lora_embedding
        mem_idx = ",".join(str(i) for i in range(28)) + ",28"
        weight_idx = ["1,2,3,4,5,6,9,10,13,14,16,17,19,20"]*28 + ["0,1"]

        start_time = time.time()
        self.model.empty_bmodel_weight(self.model_path, net_idx, mem_idx, weight_idx)
        end_time = time.time()
        print(f"\nLora Empty Time: {(end_time - start_time):.3f} s")

    def init_params(self, args):
        self.model.temperature = args.temperature
        self.model.top_p = args.top_p
        self.model.repeat_penalty = args.repeat_penalty
        self.model.repeat_last_n = args.repeat_last_n
        self.model.max_new_tokens = args.max_new_tokens
        self.model.generation_mode = args.generation_mode
        self.model.lib_path = args.lib_path
        self.model.embedding_path = args.embedding_path
        self.model.enable_lora_embedding = args.enable_lora_embedding

    def encode_tokens(self, prompt):
        messages = [
            {
                "role": "system",
                "content": self.system_prompt,
            },
            {"role": "user", "content": prompt},
        ]
        text = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        tokens = self.tokenizer(text).input_ids
        return tokens

    def stream_answer(self, tokens, inference_mode, max_tok_num):
        """
        Stream the answer for the given tokens.
        """
        tok_num = 0
        self.answer_cur = ""
        self.answer_token = []

        print()
        # First token
        first_start = time.time()
        if inference_mode == "normal":
            token = self.model.forward_first(tokens)
        else:
            raise ValueError(f"Not support {inference_mode}")
        first_end = time.time()
        # Following tokens
        while (max_tok_num > 0 and tok_num < max_tok_num) or (
            max_tok_num == 0
            and token != self.EOS
            and self.model.total_length < self.model.SEQLEN
        ):
            word = self.tokenizer.decode(token, skip_special_tokens=True)
            self.answer_token += [token]
            print(word, flush=True, end="")
            tok_num += 1
            token = self.model.forward_next()
        self.answer_cur = self.tokenizer.decode(self.answer_token)

        # counting time
        next_end = time.time()
        first_duration = first_end - first_start
        next_duration = next_end - first_end
        tps = tok_num / next_duration

        print()
        if inference_mode == "normal":
            print(f"FTL Time: {first_duration:.3f} s")
        print(f"TPS: {tps:.3f} token/s")

    def read_json(self, json_path, task_id):
        with open(json_path, "r") as file:
            text = json.load(file)
        system_str = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n"

        content_str = ""
        if "content" in text[task_id]:
            content_str = system_str + text[task_id]["content"]
        question_str = text[task_id]["question"] + "<|im_end|>\n<|im_start|>assistant\n"
        return content_str, question_str

    def get_seq_index(self, total_length, in_length):
        seq_index = []
        for index, (t_length, i_length) in enumerate(zip(self.seq_length_list, self.prefill_length_list)):
            if t_length >= total_length and i_length >= in_length:
                seq_index.append(index)
        return seq_index

    def test_sample(self):
        sample_str = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n" + "Give me a short introduction to large language model." + "<|im_end|>\n<|im_start|>assistant\n"

        # ===------------------------------------------------------------===
        # Model Init
        # ===------------------------------------------------------------===
        self.model.init_decrypt()
        self.model.prefill_reuse = 0
        self.model.stage_idx = 0
        self.load_model(self.model_path, read_bmodel=True)

        # sample 0
        in_tokens = self.tokenizer.encode(sample_str)

        in_length = len(in_tokens)
        out_length = 20
        total_length = in_length + out_length

        self.stream_answer(in_tokens, "normal", out_length)

        # ===------------------------------------------------------------===
        # Deinit
        # ===------------------------------------------------------------===
        self.model.deinit_decrypt()
        self.model.deinit()

    def test_lora(self, lora_path):
        sample_str = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n" + "Give me a short introduction to large language model." + "<|im_end|>\n<|im_start|>assistant\n"

        # ===------------------------------------------------------------===
        # Model Init
        # ===------------------------------------------------------------===
        self.model.init_decrypt()
        self.model.prefill_reuse = 0
        self.model.stage_idx = 0
        self.load_model(self.model_path, read_bmodel=True)

        # sample 0
        in_tokens = self.tokenizer.encode(sample_str)

        in_length = len(in_tokens)
        out_length = 20
        total_length = in_length + out_length

        # load lora model
        self.update_lora_and_lora_embedding(lora_path)
        self.stream_answer(in_tokens, "normal", out_length)

        # ===------------------------------------------------------------===
        # Deinit
        # ===------------------------------------------------------------===
        self.model.deinit_decrypt()
        self.model.deinit()

    def test_empty_lora(self, lora_path):
        sample_str = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n" + "Give me a short introduction to large language model." + "<|im_end|>\n<|im_start|>assistant\n"

        # ===------------------------------------------------------------===
        # Model Init
        # ===------------------------------------------------------------===
        self.model.init_decrypt()
        self.model.prefill_reuse = 0
        self.model.stage_idx = 0
        self.load_model(self.model_path, read_bmodel=True)

        # sample 0
        in_tokens = self.tokenizer.encode(sample_str)

        in_length = len(in_tokens)
        out_length = 20
        total_length = in_length + out_length

        # load lora model
        self.update_lora_and_lora_embedding(lora_path)
        self.empty_lora_and_lora_embedding()
        self.stream_answer(in_tokens, "normal", out_length)

        # ===------------------------------------------------------------===
        # Deinit
        # ===------------------------------------------------------------===
        self.model.deinit_decrypt()
        self.model.deinit()

    def test_zero_lora(self, lora_path):
        sample_str = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n" + "Give me a short introduction to large language model." + "<|im_end|>\n<|im_start|>assistant\n"

        # ===------------------------------------------------------------===
        # Model Init
        # ===------------------------------------------------------------===
        self.model.init_decrypt()
        self.model.prefill_reuse = 0
        self.model.stage_idx = 0
        self.load_model(self.model_path, read_bmodel=True)

        # sample 0
        in_tokens = self.tokenizer.encode(sample_str)

        in_length = len(in_tokens)
        out_length = 20
        total_length = in_length + out_length

        # load lora model
        self.update_lora_and_lora_embedding(lora_path)
        self.stream_answer(in_tokens, "normal", out_length)

        # ===------------------------------------------------------------===
        # Deinit
        # ===------------------------------------------------------------===
        self.model.deinit_decrypt()
        self.model.deinit()

    def test_empty_lora_with_loop(self, lora_path, loop_num):
        sample_str = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n" + "Give me a short introduction to large language model." + "<|im_end|>\n<|im_start|>assistant\n"

        # ===------------------------------------------------------------===
        # Model Init
        # ===------------------------------------------------------------===
        self.model.init_decrypt()
        self.model.prefill_reuse = 0
        self.model.stage_idx = 0
        self.load_model(self.model_path, read_bmodel=True)

        # sample 0
        in_tokens = self.tokenizer.encode(sample_str)

        in_length = len(in_tokens)
        out_length = 20
        total_length = in_length + out_length

        # load lora model
        for _ in range(loop_num):
            self.update_lora_and_lora_embedding(lora_path)
            self.empty_lora_and_lora_embedding()
            self.stream_answer(in_tokens, "normal", out_length)

        for _ in range(loop_num):
            self.update_lora_and_lora_embedding(lora_path)
            self.empty_lora_and_lora_embedding()
        self.stream_answer(in_tokens, "normal", out_length)

        # ===------------------------------------------------------------===
        # Deinit
        # ===------------------------------------------------------------===
        self.model.deinit_decrypt()
        self.model.deinit()

    def test_abnormal_length(self):
        sample_str = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n" + "Give me a short introduction to large language model." + "<|im_end|>\n<|im_start|>assistant\n"

        # ===------------------------------------------------------------===
        # Model Init
        # ===------------------------------------------------------------===
        self.model.init_decrypt()
        self.model.prefill_reuse = 0
        self.model.stage_idx = 0
        self.load_model(self.model_path, read_bmodel=True)

        # sample 0
        in_tokens = self.tokenizer.encode(sample_str)

        in_length = len(in_tokens)
        out_length = 20
        total_length = in_length + out_length

        self.stream_answer(in_tokens*1000, "normal", out_length)

        # ===------------------------------------------------------------===
        # Deinit
        # ===------------------------------------------------------------===
        self.model.deinit_decrypt()
        self.model.deinit()

    def test_abnormal_stage(self, stage_idx):
        sample_str = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n" + "Give me a short introduction to large language model." + "<|im_end|>\n<|im_start|>assistant\n"

        # ===------------------------------------------------------------===
        # Model Init
        # ===------------------------------------------------------------===
        self.model.init_decrypt()
        self.model.prefill_reuse = 0
        self.model.stage_idx = stage_idx
        self.load_model(self.model_path, read_bmodel=True)

        # sample 0
        in_tokens = self.tokenizer.encode(sample_str)

        in_length = len(in_tokens)
        out_length = 20
        total_length = in_length + out_length

        self.stream_answer(in_tokens, "normal", out_length)

        # ===------------------------------------------------------------===
        # Deinit
        # ===------------------------------------------------------------===
        self.model.deinit_decrypt()
        self.model.deinit()

    def test_abnormal_stage_2(self, stage_idx):
        sample_str = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n" + "Give me a short introduction to large language model." + "<|im_end|>\n<|im_start|>assistant\n"

        # ===------------------------------------------------------------===
        # Model Init
        # ===------------------------------------------------------------===
        self.model.init_decrypt()
        self.model.prefill_reuse = 0
        self.model.stage_idx = 0
        self.load_model(self.model_path, read_bmodel=True)

        self.model.stage_idx = stage_idx
        self.load_model(self.model_path, read_bmodel=False)

        # sample 0
        in_tokens = self.tokenizer.encode(sample_str)

        in_length = len(in_tokens)
        out_length = 20
        total_length = in_length + out_length

        self.stream_answer(in_tokens, "normal", out_length)

        # ===------------------------------------------------------------===
        # Deinit
        # ===------------------------------------------------------------===
        self.model.deinit_decrypt()
        self.model.deinit()

def get_cos_sim(torch_data_path, bmodel_data_path):
    torch_data = np.load(torch_data_path)

    seq_length = torch_data.shape[1]
    bmodel_data = np.load(bmodel_data_path)["output_0"][:,:seq_length]

    os.remove(bmodel_data_path)
    return cosine_similarity(torch_data.flatten(), bmodel_data.flatten())

"""
-1: your input is empty or exceed the maximum length
-2: can not to create handle
-3: can not to create bmrt
-4: can not to load bmodel, maybe your key is wrong
-5: can not to inference bmodel
-6: addr_mode = 0, but must set addr_mode =1
"""
def main(args):
    loop_num = 5
    dir_path = "test_lora"
    start_time = time.time()

    engine = Qwen(args)

    print("\033[31m请注意！！实际跑的时候，注释掉chat.cpp中的dump_net_output_to_file\033[0m")
    print("---------------------------(1)---------------------------")
    engine.model.enable_lora_embedding = False
    engine.test_sample()


    print("---------------------------(2)(6)(8)---------------------------")
    lora_scale_list = [0, 0.01, 0, 0.001, 0.005, 0.01]
    lora_embedding_scale_list = [0, 0, 0.01, 0.001, 0.005, 0.01]
    for lora_scale, lora_embedding_scale in zip(lora_scale_list, lora_embedding_scale_list):
        print(f"lora_scale : {lora_scale}, lora_embedding_lora : {lora_embedding_scale}")
        engine.model.enable_lora_embedding = True if lora_embedding_scale > 0 else False
        engine.test_lora(f"{dir_path}/scale{lora_scale}_embedding_scale{lora_embedding_scale}_encrypted_lora_weights.bin")
        cos_sim = get_cos_sim(f"{dir_path}/scale{lora_scale}_embedding_scale{lora_embedding_scale}_torch_hidden_states.npy", f"{dir_path}/bmodel_hidden_states.npz")
        print(f"cos_sim: {cos_sim}")
        if cos_sim < 0.8:raise ValueError("failed")

    print("---------------------------(3)(压力测试)---------------------------")
    engine.model.enable_lora_embedding = True
    engine.test_empty_lora(f"{dir_path}/scale0.005_embedding_scale0.005_encrypted_lora_weights.bin")
    engine.test_empty_lora_with_loop(f"{dir_path}/scale0.005_embedding_scale0.005_encrypted_lora_weights.bin", loop_num)

    print("---------------------------(4)---------------------------")
    engine.test_zero_lora(f"{dir_path}/scale0_embedding_scale0_encrypted_lora_weights.bin")

    # print("---------------------------测试异常长度---------------------------")
    # engine.model.enable_lora_embedding = False
    # engine.test_abnormal_length()

    # print("---------------------------测试异常stage---------------------------")
    # engine.model.enable_lora_embedding = False
    # engine.test_abnormal_stage(stage_idx=-1)
    # engine.test_abnormal_stage(stage_idx=100)
    # engine.test_abnormal_stage_2(stage_idx=-1)
    # engine.test_abnormal_stage_2(stage_idx=100)

    # print("---------------------------测试变长密钥---------------------------")
    # engine.model.enable_lora_embedding = False
    # engine.model.lib_path = "../share_cache_demo/build/libcipher_varlen.so"
    # engine.model_path = "encrypted_varlen.bmodel"
    # engine.test_sample()

    end_time = time.time()
    print(f"\nTotal Time: {(end_time - start_time):.3f} s")
    print("Status Code: ", engine.model.status_code)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--model_path", type=str, required=True, help="path to the bmodel")
    parser.add_argument('-t', '--tokenizer_path', type=str, default="../support/token_config", help='path to the tokenizer file')
    parser.add_argument('-d', '--devid', type=str, default='0', help='device ID to use')
    parser.add_argument('--temperature', type=float, default=1.0, help='temperature scaling factor for the likelihood distribution')
    parser.add_argument('--top_p', type=float, default=1.0, help='cumulative probability of token words to consider as a set of candidates')
    parser.add_argument('--repeat_penalty', type=float, default=1.2, help='penalty for repeated tokens')
    parser.add_argument('--repeat_last_n', type=int, default=32, help='repeat penalty for recent n tokens')
    parser.add_argument('--max_new_tokens', type=int, default=1024, help='max new token length to generate')
    parser.add_argument('--generation_mode', type=str, choices=["greedy", "penalty_sample"], default="greedy", help='mode for generating next token')
    parser.add_argument('--prompt_mode', type=str, choices=["prompted", "unprompted"], default="prompted", help='use prompt format or original input')
    parser.add_argument('--enable_history', action='store_true', help="if set, enables storing of history memory")
    parser.add_argument('--lib_path', type=str, default='', help='lib path by user')
    parser.add_argument('--embedding_path', type=str, default='', help='binary embedding path')
    parser.add_argument('--lora_path', type=str, default='', help='binary lora path')
    parser.add_argument('--enable_lora_embedding', action='store_true', help="if set, enables lora embedding")
    args = parser.parse_args()
    main(args)
