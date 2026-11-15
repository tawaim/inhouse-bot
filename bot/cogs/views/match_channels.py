"""The confirm step for /match-channels screenshot mode."""
from __future__ import annotations

import discord



class _ScreenshotChannelsConfirmView(discord.ui.View):
    """Ephemeral confirm step for /match-channels screenshot mode. Nothing is
    created until the admin clicks Create — same no-auto-commit rule as /report."""

    def __init__(self, cog: "AdminCog", team1: dict[str, int], team2: dict[str, int], author_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.team1 = team1
        self.team2 = team2
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the admin who ran the command can confirm.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Create channels", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        summary = await self.cog._build_team_channels(
            interaction.guild, interaction.guild_id, self.team1, self.team2,
            header="Screenshot",
        )
        await interaction.followup.send(summary, ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled — nothing created.", view=self)
