from datetime import date

from utils.academic_utils import (
    next_cycle,
    calculate_next_academic_year
)


class AcademicEngine:

    def __init__(self, db):

        self.db = db

    def get_active_period(self):

        cursor = self.db.cursor(dictionary=True)

        cursor.execute("""
            SELECT *
            FROM academic_periods
            WHERE is_active = 1
            LIMIT 1
        """)

        return cursor.fetchone()

    def should_promote(self):

        period = self.get_active_period()

        if not period:
            return False

        return date.today() > period["end_date"]

    def get_next_period_info(self):

        period = self.get_active_period()

        if not period:
            return None

        next_cycle_name = next_cycle(period["cycle"])

        next_year = calculate_next_academic_year(
            period["academic_year"],
            period["cycle"]
        )

        return {

            "academic_year": next_year,

            "cycle": next_cycle_name

        }

    def deactivate_current_period(self):

        cursor = self.db.cursor()

        cursor.execute("""

            UPDATE academic_periods

            SET

            is_active=0,

            status='Completed'

            WHERE is_active=1

        """)

    def activate_period(self, period_id):

        cursor = self.db.cursor()

        cursor.execute("""

            UPDATE academic_periods

            SET

            is_active=1,

            status='Active'

            WHERE id=%s

        """, (period_id,))

    def promote_students(self):

        cursor = self.db.cursor()

        cursor.execute("""

            UPDATE students

            SET current_semester=current_semester+1

            WHERE

            current_semester<8

            AND status='Active'

        """)

        cursor.execute("""

            UPDATE students

            SET

            status='Graduated'

            WHERE

            current_semester=8

            AND status='Active'

        """)