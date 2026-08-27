import os
import sys


def main():
    if "test" in sys.argv:
        os.environ.setdefault("DATABASE_URL", "sqlite:///test.sqlite3")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
