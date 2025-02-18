# crontab-save

Создание инкрементного копирования файлов в сетевую папку.
Протестирована работоспособность на РедОС 7.3

________________
Порядок запуска:
- chmod +x *
- ./start (если не работает, то необходимо будет запустить ./pre-start.sh)
- Идем в директорию бэкапа
- Ждем минуту
- Смотрим сетевую папку на сохранение тестовых файлов.
- Перезагружаем
- Если все работает запускаем ./lock.sh
И изменяем время crontab на:
0 13 * * *
________________


СХЕМА ВРЕМЕНИ CRONTAB:

* * * * * command(s)
- - - - -
| | | | |
| | | | ----- Day of week (0 - 7) (Sunday=0 or 7)
| | | ------- Month (1 - 12)
| | --------- Day of month (1 - 31)
| ----------- Hour (0 - 23)
------------- Minute (0 - 59)
