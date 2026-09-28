import ollama

from config import MODEL
from tools.registry import TOOLS, AVAILABLE_FUNCTIONS
from core.memory_detector import detect_memory
from memory.database import remember_fact
from voice.tts import speak


def update_context(context, function_name, arguments, result):
    if function_name in ["open_application", "close_application"]:
        name = arguments.get("name")

        if name:
            context.set_application(name)

    elif function_name == "list_files":
        folder = arguments.get("folder")

        if folder:
            context.set_folder(folder)

    elif function_name == "search_file":
        filename = arguments.get("filename")
        folder = arguments.get("folder")

        if filename:
            context.set_file(filename)

        if folder:
            context.set_folder(folder)

    elif function_name == "open_path":
        path = arguments.get("path")

        if path:
            context.set_folder(path)

    elif function_name == "open_url":
        url = arguments.get("url")

        if url:
            context.set_url(url)

    elif function_name == "search_web":
        query = arguments.get("query")

        if query:
            context.set_search(query)


def chat(messages, context, speak_response=True):
    latest_message = messages[-1]

    if latest_message["role"] == "user":
        user_text = latest_message["content"].strip().lower()

        should_check = (
            "my name is " in user_text
            or "my favorite " in user_text
            or "my favourite " in user_text
            or "i like " in user_text
            or "i love " in user_text
            or "i hate " in user_text
            or "i prefer " in user_text
            or "i dislike " in user_text
            or "i don't like " in user_text
            or "i dont like " in user_text
            or "i enjoy " in user_text
            or "i have " in user_text
            or "i live " in user_text
            or "i am " in user_text
            or "i'm " in user_text
            or "you are my " in user_text
            or "you're my " in user_text
            or "remember " in user_text
        )

        if should_check:
            try:
                detected_memory = detect_memory(
                    user_text
                )

                if detected_memory:
                    result = remember_fact(
                        detected_memory
                    )

                    if result.startswith(
                        "Remembered:"
                    ):
                        print(
                            "[Neko remembered something about you.]"
                        )

                    elif result.startswith(
                        "That memory already exists:"
                    ):
                        print(
                            "[Neko already had that memory.]"
                        )

            except Exception as e:
                print(
                    f"[Memory detector skipped: {e}]"
                )

    # ==================================================
    # ASK QWEN
    # ==================================================

    response = ollama.chat(
        model=MODEL,
        messages=messages,
        tools=TOOLS
    )

    messages.append(
        response.message
    )

    # ==================================================
    # TOOL CALLS
    # ==================================================

    if response.message.tool_calls:

        for call in response.message.tool_calls:

            function_name = call.function.name
            arguments = call.function.arguments

            if function_name in AVAILABLE_FUNCTIONS:

                if function_name in [
                    "remember_fact",
                    "search_memories"
                ]:
                    print(
                        "[Neko is checking her memories...]"
                    )

                else:
                    print(
                        "[Neko is checking your computer...]"
                    )

                result = AVAILABLE_FUNCTIONS[
                    function_name
                ](
                    **arguments
                )

                update_context(
                    context,
                    function_name,
                    arguments,
                    result
                )

                messages.append({
                    "role": "tool",
                    "tool_name": function_name,
                    "content": str(result)
                })

        # Ask Qwen again after tools.

        final_response = ollama.chat(
            model=MODEL,
            messages=messages,
            tools=TOOLS
        )

        messages.append(
            final_response.message
        )

        answer = final_response.message.content

    else:
        answer = response.message.content

    # ==================================================
    # TTS
    # ==================================================

    if speak_response:
        speak(answer)

    return answer
