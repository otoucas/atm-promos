import os
import tempfile

_tmp_dir = tempfile.mkdtemp(prefix="atm-nifty-test-")
os.environ["DATA_DIR"] = _tmp_dir
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_dir}/test.db"
os.environ["GMAIL_ADDRESS"] = ""
os.environ["GMAIL_APP_PASSWORD"] = ""
os.environ["DISABLE_GMAIL_POLLER"] = "1"

import pytest

from app.database import Base, SessionLocal, engine


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
