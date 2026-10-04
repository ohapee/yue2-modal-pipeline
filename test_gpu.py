import modal

# クラウドコンテナに PyTorch を自動インストールする設定
image = modal.Image.debian_slim().pip_install("torch")

app = modal.App("test-gpu-check", image=image)

@app.function(gpu="T4")
def check_gpu():
    import torch
    if torch.cuda.is_available():
        return f"Modal クラウドGPU接続成功: {torch.cuda.get_device_name(0)}"
    return "GPUが認識されていません"

@app.local_entrypoint()
def main():
    print("Modal クラウドGPUへ接続テストを実行中...")
    result = check_gpu.remote()
    print(result)
