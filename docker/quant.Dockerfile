# Merge + quantization image for a DGX Spark (GB10, aarch64, CUDA 13): the NGC PyTorch image plus llm-compressor
# (a vLLM-project library). Mount the repository at /lab:
#   docker run --rm --gpus all --ipc=host -v "$PWD:/lab" -v "$HOME/.cache/huggingface:/hf" -w /lab llm-serving-quant \
#     python -m llmserve.quantize quant --model runs/merged --scheme w4a16 --calib-file data/train.jsonl --system-prompt-file prompt.txt
FROM nvcr.io/nvidia/pytorch:26.10-py3

RUN pip install --no-cache-dir \
      "transformers>=4.57" "peft>=0.17" "accelerate>=1.10" "datasets>=4.0" "llmcompressor>=0.8" httpx

WORKDIR /lab
ENV HF_HOME=/hf PYTHONUNBUFFERED=1 PYTHONPATH=/lab/src
