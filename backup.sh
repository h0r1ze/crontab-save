#!/bin/bash

# Пути к файлам конфигурации
declare -r P_FOLDER="/root/.auth_smb"
declare -r P_AUTOSAMBA="/etc/auto.samba"

# Проверка и установка отсутствующих пакетов
missing_packages=()
for pkg in yad connectfolder; do
    dnf list installed "$pkg" &>/dev/null || missing_packages+=("$pkg")
done

[[ ${#missing_packages[@]} -gt 0 ]] && dnf install -y "${missing_packages[@]}"

# Форма ввода данных
ENTRY=$(yad --form --title="Настройка подключения SMB" --window-icon=featherpad --width=360 --height=150 \
    --field="Путь к сетевой папке" "192.168.0.88/share/save/123" \
    --field="Путь монтирования" "backup" \
    --field="Папка для синхронизации" "Рабочие документы" \
    --field="Имя пользователя" "" \
    --field="Домен" "SAMBA" \
    --field="Пароль:H")

[[ -z "$ENTRY" ]] && exit 0

IFS='|' read -r SMB_PATH MOUNT_PATH SYNC_FOLDER USERNAME DOMAIN PASSWORD <<< "$ENTRY"

# Выбор домашней папки пользователя
USER_HOME=$(find /home -maxdepth 1 -type d | tail -n +2 | sed 's|^/home/||' | \
    zenity --list --title="Выберите папку" --column="Папки" --height=300 --width=300)
[[ -z "$USER_HOME" ]] && { echo "Папка не выбрана. Завершаю."; exit 1; }

# Выбор папки для монтирования
MOUNT_SHARE=$(find /run/media/$USER_HOME -maxdepth 1 -type d | tail -n +2 | sed "s|^/run/media/$USER_HOME/||" | \
    zenity --list --title="Выберите папку" --column="Папки" --height=300 --width=300)
[[ -z "$MOUNT_SHARE" ]] && { echo "Сетевой ресурс не выбран. Завершаю."; exit 1; }

# Настройка autofs
if ! grep -q "/media/share    /etc/auto.samba    --ghost" /etc/auto.master; then
    echo "/media/share    /etc/auto.samba    --ghost" >> /etc/auto.master
fi

# Создание файла аутентификации
AUTH_FILE="$P_FOLDER/$(basename "$MOUNT_PATH")"
if grep -q "$MOUNT_PATH" "$P_AUTOSAMBA"; then
    yad --question --text="Подключение $MOUNT_PATH уже существует. Удалить его?"
    if [[ $? -eq 0 ]]; then
        sed -i "/$MOUNT_PATH/d" "$P_AUTOSAMBA"
        rm -f "$AUTH_FILE"
        yad --info --text="Подключение удалено."
        exec "$0"
        exit 0
    fi
    exit 0
fi

mkdir -p "$P_FOLDER"
echo -e "[smb]\nusername=$USERNAME\npassword=$PASSWORD\ndomain=$DOMAIN" > "$AUTH_FILE"
chmod 600 "$AUTH_FILE"
echo "$MOUNT_PATH -fstype=cifs,file_mode=0600,dir_mode=0700,noperm,credentials=$AUTH_FILE ://$SMB_PATH" >> "$P_AUTOSAMBA"
systemctl start autofs

# Создание пути для синхронизации
SYNC_PATH="/run/media/$USER_HOME/$MOUNT_SHARE/Рабочие документы"
mkdir -p "$SYNC_PATH"
chown "$USER_HOME":"$USER_HOME" "$SYNC_PATH"

# Создание скрипта резервного копирования
BACKUP_DIR="/home/$USER_HOME/.local/share/.backup-script"
mkdir -p "$BACKUP_DIR"
BACKUP_SCRIPT="$BACKUP_DIR/crontab-script.sh"

cat > "$BACKUP_SCRIPT" <<EOF
#!/bin/bash
set -o errexit
set -o nounset
set -o pipefail

readonly SOURCE_DIR="$SYNC_PATH/"
readonly BACKUP_DIR="/media/share/backup/"

rsync -av "\${SOURCE_DIR}/" --include="*.doc" --include="*.docx" --include="*.odt" \
            --include="*.rtf" --include="*.txt" --include="*.xls" --include="*.xlsx" \
            --include="*.ods" --include="*.csv" --include="*.ppt" --include="*.pptx" \
            --include="*.pdf" --include="*.rar" --include="*.zip" --include="*.7z" \
            --include="*.tar*" --include="*.bmp" --include="*.jpg" --include="*.jpeg" \
            --include="*.png" --include="*.gif" --include="*.chm" --include="*.html" \
            --include="*.eml" --include="*.lnk" --exclude="*" "\${BACKUP_DIR}"
EOF

chmod +x "$BACKUP_SCRIPT"
chown "$USER_HOME":"$USER_HOME" "$BACKUP_SCRIPT"

echo "* * * * * /bin/bash $BACKUP_SCRIPT" | crontab -u "$USER_HOME" -

yad --info --text="Подключение добавлено и настроено успешно!"
