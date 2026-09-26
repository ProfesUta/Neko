import ollama

from config import MODEL
from tools.registry import TOOLS, AVAILABLE_FUNCTIONS
from core.memory_detector import detect_memory
from memory.database import remember_fact



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


def chat(messages, context):
    # Check whether the user's latest message contains
    # a useful long-term memory.
    latest_message = messages[-1]

    if latest_message["role"] == "user":
        user_text = latest_message["content"]

        try:
            detected_memory = detect_memory(user_text)

            if detected_memory:
                result = remember_fact(detected_memory)

                if result.startswith("Remembered:"):
                    print("[Neko remembered something about you.]")
                elif result.startswith("That memory already exists:"):
                    print("[Neko already had that memory.]")



        except Exception as e:
            print(f"[Memory detector skipped: {e}]")

    response = ollama.chat(
        model=MODEL,
        messages=messages,
        tools=TOOLS
    )

    messages.append(response.message)

    if response.message.tool_calls:
        for call in response.message.tool_calls:
            function_name = call.function.name
            arguments = call.function.arguments


            if function_name in AVAILABLE_FUNCTIONS:

                if function_name in ["remember_fact", "search_memories"]:
                    print("[Neko is checking her memories...]")
                else:
                    print("[Neko is checking your computer...]")

                result = AVAILABLE_FUNCTIONS[function_name](**arguments)

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

        final_response = ollama.chat(
            model=MODEL,
            messages=messages,
            tools=TOOLS
        )

        messages.append(final_response.message)

        return final_response.message.content

    return response.message.content
