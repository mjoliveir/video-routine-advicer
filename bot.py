import os
import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN não foi configurado no arquivo .env")


with open("config.json", "r", encoding="utf-8") as file:
    config = json.load(file)


CHANNEL_NAME = config["channel_name"]
TIMEZONE = ZoneInfo(config["timezone"])
SCHEDULES = config["schedules"]

intents = discord.Intents.default()
intents.guilds = True


bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


already_sent = set()


def get_current_time():
    return datetime.now(TIMEZONE)


def get_schedule(time):
    for schedule in SCHEDULES:
        if schedule["time"] == time:
            return schedule

    return None


def get_draft_time(schedule):
    """
    Horário em que o vídeo agendado sai no YouTube (e em que os rascunhos do
    TikTok/Instagram precisam ser postados). Sai do campo "schedule":
    "12:30" -> "12:30", "07:00 de amanhã" -> "07:00".
    """
    match = re.search(r"(\d{1,2}):(\d{2})", schedule.get("schedule", ""))

    if match is None:
        return None

    return f"{int(match.group(1)):02d}:{match.group(2)}"


def get_draft_role_name(schedule):
    # Por padrão avisa o mesmo cargo que agendou. Para avisar outro cargo,
    # coloque "draft_role" no item da escala no config.json.
    return schedule.get("draft_role", schedule["role"])


def build_draft_message(role, schedule):
    return (
        f"📲 {role.mention}\n\n"
        f"**Hora de postar os rascunhos!**\n\n"
        f"📺 O vídeo das **{get_draft_time(schedule)}** está saindo agora "
        f"no YouTube (agendado)\n"
        f"📤 Poste agora o rascunho dele no **TikTok** e no **Instagram**"
    )


async def send_schedule(schedule, reminder=False):
    for guild in bot.guilds:
        channel = discord.utils.get(
            guild.text_channels,
            name=CHANNEL_NAME
        )

        if channel is None:
            print(
                f"[AVISO] Canal #{CHANNEL_NAME} não encontrado "
                f"no servidor {guild.name}"
            )
            continue

        role = discord.utils.get(
            guild.roles,
            name=schedule["role"]
        )

        if role is None:
            print(
                f"[AVISO] Cargo '{schedule['role']}' não encontrado "
                f"no servidor {guild.name}"
            )
            continue

        if reminder:
            message = (
                f"🔔 {role.mention}\n\n"
                f"**Lembrete: faltam 10 minutos!**\n\n"
                f"📤 Prepare o vídeo das **{schedule['post']}**\n"
                f"📅 Agende o vídeo das **{schedule['schedule']}**"
            )
        else:
            message = (
                f"🚨 {role.mention}\n\n"
                f"**Hora da postagem!**\n\n"
                f"📤 Poste o vídeo das **{schedule['post']}**\n"
                f"📅 Agende o vídeo das **{schedule['schedule']}**"
            )

        await channel.send(
            message,
            allowed_mentions=discord.AllowedMentions(
                roles=True
            )
        )

        print(
            f"[POSTAGEM] {schedule['role']} "
            f"{'lembrete' if reminder else 'aviso'} "
            f"enviado em {guild.name}"
        )


async def send_draft_reminder(schedule):
    for guild in bot.guilds:
        channel = discord.utils.get(
            guild.text_channels,
            name=CHANNEL_NAME
        )

        if channel is None:
            print(
                f"[AVISO] Canal #{CHANNEL_NAME} não encontrado "
                f"no servidor {guild.name}"
            )
            continue

        role_name = get_draft_role_name(schedule)

        role = discord.utils.get(
            guild.roles,
            name=role_name
        )

        if role is None:
            print(
                f"[AVISO] Cargo '{role_name}' não encontrado "
                f"no servidor {guild.name}"
            )
            continue

        await channel.send(
            build_draft_message(role, schedule),
            allowed_mentions=discord.AllowedMentions(
                roles=True
            )
        )

        print(
            f"[RASCUNHO] aviso dos rascunhos das "
            f"{get_draft_time(schedule)} enviado em {guild.name}"
        )


@tasks.loop(seconds=1)
async def scheduler():
    now = get_current_time()

    current_time = now.strftime("%H:%M")
    current_date = now.strftime("%Y-%m-%d")

    schedule = get_schedule(current_time)

    if schedule is not None:
        key = f"{current_date}_{current_time}"

        if key not in already_sent:
            already_sent.add(key)
            await send_schedule(schedule)

    # Hora de postar os rascunhos do TikTok/Instagram (o que foi agendado
    # no YouTube está saindo agora)
    for schedule in SCHEDULES:
        draft_time = get_draft_time(schedule)

        if draft_time == current_time:
            key = f"{current_date}_{draft_time}_draft"

            if key not in already_sent:
                already_sent.add(key)
                await send_draft_reminder(schedule)

    reminder_time = (
        now.replace(second=0, microsecond=0)
    )

    for schedule in SCHEDULES:
        hour, minute = map(int, schedule["time"].split(":"))

        target_time = now.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0
        )

        difference = target_time - reminder_time

        if difference.total_seconds() == 600:
            key = f"{current_date}_{schedule['time']}_reminder"

            if key not in already_sent:
                already_sent.add(key)
                await send_schedule(
                    schedule,
                    reminder=True
                )


@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print("=" * 50)
    print(f"Bot conectado como: {bot.user}")
    print(f"Servidores: {len(bot.guilds)}")
    print(f"Canal: #{CHANNEL_NAME}")
    print("Horário: America/Sao_Paulo")

    print("=" * 50)

    try:
        synced = await bot.tree.sync()
        print(f"Comandos sincronizados: {len(synced)}")
    except Exception as e:
        print(f"Erro ao sincronizar comandos: {e}")

    if not scheduler.is_running():
        scheduler.start()


@bot.tree.command(
    name="status",
    description="Mostra o próximo aviso da escala"
)
async def status(interaction: discord.Interaction):
    now = get_current_time()

    upcoming = []

    # Candidatos de hoje e de amanhã (timedelta evita quebrar no fim do mês)
    for day_offset in (0, 1):
        day = now + timedelta(days=day_offset)

        for schedule in SCHEDULES:
            hour, minute = map(int, schedule["time"].split(":"))

            candidate = day.replace(
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0
            )

            if candidate > now:
                upcoming.append((candidate, schedule, "post"))

            draft_time = get_draft_time(schedule)

            if draft_time:
                hour, minute = map(int, draft_time.split(":"))

                candidate = day.replace(
                    hour=hour,
                    minute=minute,
                    second=0,
                    microsecond=0
                )

                if candidate > now:
                    upcoming.append((candidate, schedule, "draft"))

    next_time, schedule, kind = min(
        upcoming,
        key=lambda item: item[0]
    )

    if kind == "draft":
        await interaction.response.send_message(
            f"📲 **Próximo aviso: rascunhos do TikTok e Instagram**\n\n"
            f"⏰ Horário: **{get_draft_time(schedule)}**\n"
            f"👤 Cargo: {get_draft_role_name(schedule)}\n"
            f"📺 Sai agendado no YouTube: vídeo das "
            f"**{get_draft_time(schedule)}**\n\n"
            f"Próximo aviso: <t:{int(next_time.timestamp())}:R>"
        )
        return

    await interaction.response.send_message(
        f"📅 **Próxima postagem**\n\n"
        f"⏰ Horário: **{schedule['time']}**\n"
        f"👤 Cargo: {schedule['role']}\n"
        f"📤 Postar: **{schedule['post']}**\n"
        f"📅 Agendar: **{schedule['schedule']}**\n\n"
        f"Próximo aviso: <t:{int(next_time.timestamp())}:R>"
    )


@bot.tree.command(
    name="teste",
    description="Testa um aviso de postagem (ou o aviso dos rascunhos)"
)
async def teste(
    interaction: discord.Interaction,
    horario: str,
    rascunho: bool = False
):
    schedule = get_schedule(horario)

    if schedule is None:
        await interaction.response.send_message(
            "❌ Horário inválido. Use `10:00`, `15:30` ou `21:30`.",
            ephemeral=True
        )
        return

    if rascunho and get_draft_time(schedule) is None:
        await interaction.response.send_message(
            "❌ Esse horário não tem horário de agendamento no campo `schedule`.",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    channel = discord.utils.get(
        interaction.guild.text_channels,
        name=CHANNEL_NAME
    )

    if channel is None:
        await interaction.followup.send(
            f"❌ Canal `#{CHANNEL_NAME}` não encontrado.",
            ephemeral=True
        )
        return

    role_name = (
        get_draft_role_name(schedule) if rascunho else schedule["role"]
    )

    role = discord.utils.get(
        interaction.guild.roles,
        name=role_name
    )

    if role is None:
        await interaction.followup.send(
            f"❌ Cargo `{role_name}` não encontrado.",
            ephemeral=True
        )
        return

    if rascunho:
        message = build_draft_message(role, schedule)
    else:
        message = (
            f"🔔 {role.mention}\n\n"
            f"**Hora da postagem!**\n\n"
            f"📤 Poste o vídeo das **{schedule['post']}**\n"
            f"📅 Agende o vídeo das **{schedule['schedule']}**"
        )

    await channel.send(
        message,
        allowed_mentions=discord.AllowedMentions(
            roles=True
        )
    )

    await interaction.followup.send(
        "✅ Aviso enviado.",
        ephemeral=True
    )


@bot.tree.command(
    name="escala",
    description="Mostra todos os horários da escala"
)
async def escala(interaction: discord.Interaction):
    text = "📋 **ESCALA DE POSTAGEM**\n\n"

    for schedule in SCHEDULES:
        text += (
            f"⏰ **{schedule['time']}**\n"
            f"👤 {schedule['role']}\n"
            f"📤 Postar: **{schedule['post']}**\n"
            f"📅 Agendar: **{schedule['schedule']}**\n"
        )

        draft_time = get_draft_time(schedule)

        if draft_time:
            text += (
                f"📲 Rascunhos TikTok/Instagram: **{draft_time}**\n"
            )

        text += "\n"

    await interaction.response.send_message(text)


bot.run(TOKEN)