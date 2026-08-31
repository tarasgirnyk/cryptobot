"""Точка входу CryptoBOT.

Уся логіка живе в пакеті ``cryptobot/``. Цей файл лишається, щоб працювали
``python server.py``, start.bat і Dockerfile без змін.
"""

from cryptobot.app import main


if __name__ == "__main__":
    main()
