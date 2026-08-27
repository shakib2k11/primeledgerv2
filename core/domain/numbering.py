from datetime import date, datetime


MAX_ANNUAL_SEQUENCE = 999_999


def reference_year(value: date | datetime) -> int:
    year = value.year
    if year < 2000 or year > 2099:
        raise ValueError("Automatic reference numbers support years from 2000 through 2099.")
    return year


def format_reference_number(year: int, sequence: int) -> str:
    if year < 2000 or year > 2099:
        raise ValueError("Automatic reference numbers support years from 2000 through 2099.")
    if sequence < 1 or sequence > MAX_ANNUAL_SEQUENCE:
        raise ValueError("The annual reference sequence must be between 1 and 999999.")
    return f"{year % 100:02d}{sequence:06d}"
