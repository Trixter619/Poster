"""Interactive first-admin setup; no passwords in arguments or logs."""
import getpass
import os
from operators import Operators


def main():
    users = Operators(os.environ.get('VK_POSTER_DATA', 'data'))
    if users.users():
        print('Администратор уже создан. Учётные записи сохранены.')
        return
    username = input('Логин первого администратора: ').strip()
    password = getpass.getpass('Пароль: ')
    if password != getpass.getpass('Повтори пароль: '):
        raise SystemExit('Пароли не совпадают.')
    try:
        users.add(username, password, first=True)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print('Администратор создан.')


if __name__ == '__main__':
    main()
