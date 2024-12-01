import unittest

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from database.models import init_db


class BaseTest(unittest.TestCase):

    @classmethod
    async def asyncSetUpClass(cls):
        pass

    @classmethod
    async def asyncTearDownClass(cls):
        pass

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await init_db(self.engine)
        self.Session = sessionmaker(bind=self.engine, class_=AsyncSession)
        self.session = self.Session()

    async def asyncTearDown(self):
        self.session.close()
        self.engine.dispose()
