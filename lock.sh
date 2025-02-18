#!/bin/bash
USERNAME=`zenity --forms --width=200 --height=100 --title="Пользовательские данные" \
	--text="Введите имя 
домашнего пользователя" \
	--add-entry=""`
chmod go-rwx /home/$USERNAME/.local/share/.backup-script
chmod go-rwx /home/$USERNAME/.local/share/.backup-script/crontab-script.sh
chown root:root /home/$USERNAME/.local/share/.backup-script
chown root:root /home/$USERNAME/.local/share/.backup-script/crontab-script.sh
gpasswd -d $USERNAME wheel

zenity --warning --width=300 \
--text="
Сейчас перед вами откроется crontab, для включения скрипта в:
13:00 ежедневно, необходимо 
заменить: * * * * * 
на: 0 13 * * *
Чтобы протестировать как работает скрипт: *\1 * * * *"
EDITOR=nano crontab -e
