from datetime import date


ODD_SEMESTERS = [1, 3, 5, 7]
EVEN_SEMESTERS = [2, 4, 6, 8]


def get_cycle_semesters(cycle: str):
    """
    Returns the semesters belonging to a cycle.
    """

    if not cycle:
        return []

    cycle = cycle.upper()

    if cycle == "ODD":
        return ODD_SEMESTERS

    if cycle == "EVEN":
        return EVEN_SEMESTERS

    return []


def is_semester_allowed(cycle: str, semester: int):

    return semester in get_cycle_semesters(cycle)


def next_cycle(cycle: str):

    cycle = cycle.upper()

    if cycle == "ODD":
        return "EVEN"

    return "ODD"


def cycle_display(cycle):

    if cycle.upper() == "ODD":
        return "Semesters 1 • 3 • 5 • 7"

    return "Semesters 2 • 4 • 6 • 8"


def calculate_next_academic_year(year_text, cycle):

    """
    Examples

    2026-27 Odd -> 2026-27 Even

    2026-27 Even -> 2027-28 Odd
    """

    start = int(year_text[:4])

    end = int(year_text[-2:])

    if cycle.upper() == "ODD":
        return year_text

    start += 1
    end += 1

    return f"{start}-{str(end)[-2:]}"