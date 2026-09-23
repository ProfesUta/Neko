import ollama

from config import MODEL
from tools import TOOLS, AVAILABLE_FUNCTIONS


def chat(messages):

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

                print("[Neko is checking your computer...]")

                result = AVAILABLE_FUNCTIONS[
                    function_name
                ](**arguments)


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