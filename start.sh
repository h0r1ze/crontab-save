#!/bin/bash
SERVERID=`zenity --forms --width=200 --height=100 --title="Пользовательские данные" \
	--text="Пример ввода полей:

1) 10.82.107.3/inv/zal/Имя папки
2) /mnt/backup (Создается автоматически после ввода)
3) Документы (Данная папка будет создана на рабочем столе)

__________________________________________________________________________________________________________________________________________
Рекомендации:
Если в имени папки присутствует пробел, его необходимо заменить символом или убрать." \
	--add-entry="Путь сетевой папки" \
  --add-entry="Путь монтирования папки" \
  --add-entry="Название папки которая будет синхронизироваться с сетевой папкой"`

zenity --info --width=350 \
--text="
Для входа в сетевую папку необходимо 
ввести имя пользователя и пароль."

dnf install yad --assumeno > infoupdate.txt
clear

InfoYadInstallation=`cat infoupdate.txt | grep -o "уже установлен"`

if [[ $InfoYadInstallation = "уже установлен" ]]; then
  true
else
  dnf install yad -y
fi
rm infoupdate.txt

ENTRY=`yad --form --window-icon=featherpad \
--title "Авторизация" \
--text="Введите данные для подключения к сетевой папке" \
    --field=Имя\ пользователя ""\
    --field=Домен "SAMBA"\
    --field=Пароль:H`

case $? in
0)
echo $ENTRY | cut -d'|' -f1 > user.txt
echo $ENTRY | cut -d'|' -f2 > domain.txt
echo $ENTRY | cut -d'|' -f3 > password.txt
echo $SERVERID | cut -d'|' -f1 > smbfolder.txt
echo $SERVERID | cut -d'|' -f2 > foldermnt.txt
echo $SERVERID | cut -d'|' -f3 > CreateFolderDesktop.txt
SmbfolderServer=`cat smbfolder.txt`
FolderMnt=`cat foldermnt.txt`
UserNameServer=`cat user.txt`
DomainServer=`cat domain.txt`
PassWordServer=`cat password.txt`
CreateFolderDesktop=`cat CreateFolderDesktop.txt`

USERNAME=`zenity --forms --width=200 --height=100 --title="Пользовательские данные" \
  --text="" \
  --add-entry="Введите имя
домашнего пользователя"`

usermod -aG wheel $USERNAME
mkdir $FolderMnt
sudo -u $USERNAME mkdir /home/$USERNAME/Рабочий\ стол/$CreateFolderDesktop
sudo -u $USERNAME mkdir /home/$USERNAME/.local/share/.backup-script

echo 'sudo -u root mount -t cifs '//$SmbfolderServer' '$FolderMnt' -o user='$UserNameServer',pass='$PassWordServer,domain=$DomainServer'
mkdir '$FolderMnt'/logfile 2> /dev/null
set -o errexit
set -o nounset
set -o pipefail
#                                         Директория документа
readonly SOURCE_DIR='\"/home/$USERNAME/Рабочий стол/$CreateFolderDesktop\"'
readonly BACKUP_DIR="'$FolderMnt'"
readonly DATETIME="$(date '\'+%Y-%m-%d_%H:%M:%S\'')"
readonly BACKUP_PATH="${BACKUP_DIR}"

mkdir -p "${BACKUP_DIR}"

rsync -av \
"${SOURCE_DIR}/" \
--exclude=".cache" \
"${BACKUP_PATH}" > '$FolderMnt'/logfile/logfile-$DATETIME.txt
umount -f '$FolderMnt'' > /home/$USERNAME/.local/share/.backup-script/crontab-script.sh
  
export EDITOR=nano
echo "#* * * * * command(s)
#- - - - -
#| | | | |
#| | | | ----- Day of week (0 - 7) (Sunday=0 or 7)
#| | | ------- Month (1 - 12)
#| | --------- Day of month (1 - 31)
#| ----------- Hour (0 - 23)
#------------- Minute (0 - 59)

* * * * * /home/$USERNAME/.local/share/.backup-script/crontab-script.sh" | xsel -b -i

zenity --warning --width=300 \
--text="
Настройки crontab скопированы!
После открытия nano нажмите shift+crtl+v"
chmod +x /home/$USERNAME/.local/share/.backup-script/crontab-script.sh
chown $USERNAME:$USERNAME /home/$USERNAME/.local/share/.backup-script/crontab-script.sh
EDITOR=nano crontab -e

zenity --warning --width=300 \
--text="
Можно проверять работоспособноть.

Для корректной работы необходимо перезагрузить ПК и запустить второй скрипт для закрытия доступа к редактированию файла."

rm user.txt
rm password.txt
rm smbfolder.txt
rm domain.txt
rm foldermnt.txt
rm CreateFolderDesktop.txt
          ;;
         1)
                echo "Ошибка авторизации.";;
        -1)
                echo "Неизвестная ошибка.";;
esac
