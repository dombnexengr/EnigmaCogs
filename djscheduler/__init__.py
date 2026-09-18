from redbot.core.utils import get_end_user_data_statement

from .djscheduler import DJScheduler

__red_end_user_data_statement__ = get_end_user_data_statement(__file__)


async def setup(bot) -> None:
    await bot.add_cog(DJScheduler(bot))
