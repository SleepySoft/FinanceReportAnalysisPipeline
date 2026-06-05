import json
import ollama


def test_json_lock():
    prompt = """
    请提取以下文本的目录，必须输出包含 "sections" 的 JSON 对象。
    文本：第一节 目录，第二节 正文，第三节 财务。
    """

    response = ollama.chat(
        # 1. 换成你刚才打包出的“哑巴”模型
        model='qwen2.5:7b-instruct-q8_0-json',
        messages=[{'role': 'user', 'content': prompt}],
        # 2. 双保险：在 API 层面再次强制 JSON 约束
        format='json',
        options={
            "num_ctx": 8192
            # 注意：这里不需要再写 temperature，因为 Modelfile 里已经锁死了为 0
        }
    )

    raw_output = response['message']['content']
    print("模型原始输出如下：")
    print("---")
    print(raw_output)
    print("---")

    try:
        parsed = json.loads(raw_output)
        print("✅ JSON 解析成功！没有废话。")
    except Exception as e:
        print(f"❌ JSON 解析失败，说明它还是加了废话: {e}")

# 运行测试
test_json_lock()