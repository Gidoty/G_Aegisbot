import warnings

from sqlalchemy.orm import DeclarativeBase

# SQLite has no native Decimal type; values are stored as floats. The schema uses Numeric
# so a later move to Postgres keeps exact decimals.
warnings.filterwarnings("ignore", message=".*does \\*not\\* support Decimal objects natively.*")


class Base(DeclarativeBase):
    pass
