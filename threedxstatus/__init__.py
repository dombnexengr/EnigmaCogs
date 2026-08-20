import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redbot.core.bot import Red

with open(Path(__file__).parent / "info.json", encoding="utf-8") as fp:
    __red_end_user_data_statement__ = json.load(fp)["end_user_data_statement"]


async def setup(bot: "Red") -> None:
    from .threedxstatus import ThreeDXStatus

    await bot.add_cog(ThreeDXStatus(bot))
