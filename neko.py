import ollama

while True:
    user = input("You: ")

    if user.lower() == "exit":
        break

    response = ollama.chat(
        model="qwen3:14b",
        messages=[
            {
                "role": "user",
                "content": user
            }
        ]
    )

    print("Neko:", response["message"]["content"])