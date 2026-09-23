class Context:
    def __init__(self):
        self.last_application = None
        self.last_folder = None
        self.last_file = None
        self.last_url = None
        self.last_search = None

    def set_application(self, application):
        self.last_application = application

    def set_folder(self, folder):
        self.last_folder = folder

    def set_file(self, file):
        self.last_file = file

    def set_url(self, url):
        self.last_url = url

    def set_search(self, search):
        self.last_search = search

    def get_summary(self):
        return {
            "last_application": self.last_application,
            "last_folder": self.last_folder,
            "last_file": self.last_file,
            "last_url": self.last_url,
            "last_search": self.last_search,
        }

    def get_text(self):
        summary = self.get_summary()

        lines = []

        if summary["last_application"]:
            lines.append(
                f"Last application: {summary['last_application']}"
            )

        if summary["last_folder"]:
            lines.append(
                f"Last folder: {summary['last_folder']}"
            )

        if summary["last_file"]:
            lines.append(
                f"Last file: {summary['last_file']}"
            )

        if summary["last_url"]:
            lines.append(
                f"Last URL: {summary['last_url']}"
            )

        if summary["last_search"]:
            lines.append(
                f"Last web search: {summary['last_search']}"
            )

        if not lines:
            return "No recent context."

        return "\n".join(lines)