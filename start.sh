#!/bin/bash
P_FOLDER="/root/.auth_smb"
P_AUTOSAMBA="/etc/auto.samba"

missing_packages=()
for pkg in yad connectfolder; do
    dnf list installed "$pkg" &>/dev/null || missing_packages+=("$pkg")
done

[ ${#missing_packages[@]} -gt 0 ] && {
    dnf install -y "${missing_packages[@]}" | zenity --progress --title="Установка компонентов" --text="Идет установка..." --percentage=0 --auto-close --auto-kill
}

ENTRY=$(yad --form --title="Настройка подключения SMB" --window-icon=featherpad --width=360 --height=150 \
--field="Путь к сетевой папке" "192.168.0.55/folder" \
--field="Путь монтирования" "backup" \
--field="Папка для синхронизации" "Рабочие документы" \
--field="Имя пользователя" "" \
--field="Домен" "SAMBA" \
--field="Пароль:H")

[ -z "$ENTRY" ] && exit 0
IFS='|' read -r SMB_PATH MOUNT_PATH SYNC_FOLDER USERNAME DOMAIN PASSWORD <<< "$ENTRY"
USER_HOME=$(find /home -maxdepth 1 -type d | tail -n +2 | sed 's|^/home/||' | zenity --list --title="Выберите папку" --column="Папки" --height=300 --width=300)
[ -z "$USER_HOME" ] && { echo "Папка не выбрана. Завершаю."; exit 1; }

# Создаем /etc/auto.samba, если его нет
touch "$P_AUTOSAMBA"

# Проверка на существование строки в /etc/auto.master
if ! grep -q "/media/share    /etc/auto.samba    --ghost" /etc/auto.master; then
    echo "/media/share    /etc/auto.samba    --ghost" >> /etc/auto.master
fi

AUTH_FILE="$P_FOLDER/$(basename "$MOUNT_PATH")"
grep -q "$MOUNT_PATH" "$P_AUTOSAMBA" && {
    yad --question --text="Подключение $MOUNT_PATH уже существует. Удалить его?"
    [ $? -eq 0 ] && { sed -i "/$MOUNT_PATH/d" "$P_AUTOSAMBA"; rm -f "$AUTH_FILE"; yad --info --text="Подключение удалено."; exec "$0"; exit 0; }
    exit 0
}

mkdir -p "$P_FOLDER"
echo -e "[smb]\nusername=$USERNAME\npassword=$PASSWORD\ndomain=$DOMAIN" > "$AUTH_FILE"
chmod 600 "$AUTH_FILE"
echo "$MOUNT_PATH -fstype=cifs,file_mode=0600,dir_mode=0700,noperm,credentials=$AUTH_FILE ://$SMB_PATH" >> "$P_AUTOSAMBA"
usermod -aG wheel "$USER_HOME"

# Перезапуск autofs и ожидание его работы
systemctl enable --now autofs
sleep 2  # Даем время на применение настроек
automount -fv
sleep 2  # Дополнительное ожидание после automount

# Создаем только нужную папку для синхронизации
SYNC_PATH="/home/$USER_HOME/Рабочий стол/$SYNC_FOLDER"
mkdir -p "$SYNC_PATH"
chown "$USER_HOME":"$USER_HOME" "$SYNC_PATH"

# Создаем каталог для скрипта резервного копирования
BACKUP_DIR="/home/$USER_HOME/.local/share/.backup-script"
mkdir -p "$BACKUP_DIR"

BACKUP_SCRIPT="$BACKUP_DIR/crontab-script.sh"

cat > "$BACKUP_SCRIPT" <<EOF
set -o errexit
set -o nounset
set -o pipefail
mkdir -p "/media/share/backup/logfile"
readonly SOURCE_DIR="$SYNC_PATH/"
readonly BACKUP_DIR="/media/share/backup/"
readonly DATETIME="\$(date '+%Y-%m-%d_%H:%M:%S')"
readonly BACKUP_PATH="\${BACKUP_DIR}"
rsync -av \
"\${SOURCE_DIR}/" \
--exclude=".cache" \
"\${BACKUP_PATH}" > "/media/share/backup/logfile/logfile-\$DATETIME.txt"
EOF

chmod +x "$BACKUP_SCRIPT"
chown "$USER_HOME":"$USER_HOME" "$BACKUP_SCRIPT"
echo "* * * * * /bin/bash $BACKUP_SCRIPT" | crontab -u "$USER_HOME" -
yad --info --text="Подключение добавлено и настроено успешно!"
