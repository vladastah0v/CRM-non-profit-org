# Даты в формате ДД.ММ.ГГГГ во всём интерфейсе (п. 3.10 ТЗ)
DATE_FORMAT = "d.m.Y"
SHORT_DATE_FORMAT = "d.m.Y"
DATETIME_FORMAT = "d.m.Y H:i"
SHORT_DATETIME_FORMAT = "d.m.Y H:i"
TIME_FORMAT = "H:i"
MONTH_DAY_FORMAT = "j F"
YEAR_MONTH_FORMAT = "F Y"
FIRST_DAY_OF_WEEK = 1

DATE_INPUT_FORMATS = ["%d.%m.%Y", "%Y-%m-%d"]
DATETIME_INPUT_FORMATS = ["%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"]
TIME_INPUT_FORMATS = ["%H:%M", "%H:%M:%S"]
DECIMAL_SEPARATOR = ","
THOUSAND_SEPARATOR = "\xa0"
NUMBER_GROUPING = 3
