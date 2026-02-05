import discord
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import pytz
import json
import os
import random
import threading
from flask import Flask

# ---------------- CONFIG ---------------- #
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN not set")

PORT = int(os.getenv("PORT", 8080))

TIMEZONE = pytz.timezone("Asia/Manila")
AM_IN_CUTOFF = (10, 0)
REQUIRED_HOURS = 8
MORNING_PERSON_CUTOFF = (7, 44)
# ---------------------------------------- #

# --- Google Sheets Setup ---
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

cred_path = "/etc/secrets/google_credentials.json"
creds = ServiceAccountCredentials.from_json_keyfile_name(cred_path, scope)
client = gspread.authorize(creds)
sheet = client.open("DTR HAWKS").sheet1
# --------------------------------

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
# --------------------------------

# --- Simple Flask App for Health Checks ---
app = Flask(__name__)


@app.route('/')
@app.route('/health')
def health_check():
    return 'OK', 200


def run_flask():
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(host='0.0.0.0', port=PORT, debug=False, use_reloader=False)
# --------------------------------


# ---------------- USERS STORAGE ---------------- #
if os.path.exists("/etc/secrets"):
    USERS_FILE = "/etc/secrets/users.json"
    ADMINS_FILE = "/etc/secrets/admins.json"
else:
    USERS_FILE = "users.json"
    ADMINS_FILE = "admins.json"

if os.path.exists(USERS_FILE):
    with open(USERS_FILE, "r") as f:
        user_names = json.load(f)
else:
    user_names = {}
    if not USERS_FILE.startswith("/etc/secrets"):
        with open(USERS_FILE, "w") as f:
            json.dump(user_names, f, indent=4)

if os.path.exists(ADMINS_FILE):
    with open(ADMINS_FILE, "r") as f:
        admin_data = json.load(f)
        ADMIN_IDS = admin_data.get("admin_ids", [])
else:
    ADMIN_IDS = []
    if not ADMINS_FILE.startswith("/etc/secrets"):
        with open(ADMINS_FILE, "w") as f:
            json.dump({"admin_ids": []}, f, indent=4)
# ----------------------------------------

# ---------------- HELPERS ---------------- #


def strip_leading_apostrophe(s: str) -> str:
    if s.startswith("'"):
        return s[1:]
    return s


def now():
    return datetime.now(TIMEZONE)


def today():
    n = now()
    return f"{n.month}/{n.day}/{n.year}"


def pretty_date():
    return now().strftime("%B %d, %Y")


def timestamp_str():
    n = now()
    return f"{n.month}/{n.day}/{n.year} {n.hour}:{n.minute:02d}:{n.second:02d}"


def time_for_sheets():
    n = now()
    hour_12 = n.hour % 12
    if hour_12 == 0:
        hour_12 = 12
    am_pm = "AM" if n.hour < 12 else "PM"
    return f"{hour_12}:{n.minute:02d}:00 {am_pm}"


def time_for_discord():
    n = now()
    hour_12 = n.hour % 12
    if hour_12 == 0:
        hour_12 = 12
    am_pm = "AM" if n.hour < 12 else "PM"
    return f"{hour_12}:{n.minute:02d} {am_pm}"


def format_name_with_initials(full_name):
    parts = full_name.strip().split()
    if len(parts) < 2:
        return full_name

    last_name = parts[-1]

    if len(parts) == 2:
        first_name = parts[0]
        return f"{first_name} C. {last_name}"

    first_name = parts[0]
    middle_parts = parts[1:-1]
    last_middle = middle_parts[-1]
    middle_initial = last_middle[0].upper()

    if len(middle_parts) > 1:
        remaining_middle = " ".join(middle_parts[:-1])
        return f"{first_name} {remaining_middle} {middle_initial}. {last_name}"
    else:
        return f"{first_name} {middle_initial}. {last_name}"


def get_user_name(ctx):
    uid = str(ctx.author.id)
    raw_name = user_names.get(uid, None)
    if raw_name:
        return format_name_with_initials(raw_name)
    return None


def is_late():
    late_time = now().replace(hour=AM_IN_CUTOFF[0], minute=AM_IN_CUTOFF[1],
                              second=0, microsecond=0)
    return now() >= late_time


def parse_time_from_string(time_string):
    if not time_string:
        return None
    try:
        time_string = time_string.strip()
        formats = [
            "%I:%M:%S %p",
            "%H:%M:%S",
            "%I:%M %p",
        ]
        for fmt in formats:
            try:
                t = datetime.strptime(time_string, fmt).time()
                return datetime.combine(now().date(), t)
            except ValueError:
                continue
        return None
    except Exception as e:
        print(f"Error parsing time '{time_string}': {e}")
        return None


def validate_time_sequence(record):
    times = {}
    for key, val in record.items():
        if val:
            parsed = parse_time_from_string(val)
            if parsed:
                times[key] = parsed

    if "AM_IN" in times and "AM_OUT" in times:
        if times["AM_OUT"] <= times["AM_IN"]:
            return False, "AM OUT must be after AM IN."

    if "AM_OUT" in times and "PM_IN" in times:
        if times["PM_IN"] <= times["AM_OUT"]:
            return False, "PM IN must be after AM OUT."

    if "PM_IN" in times and "PM_OUT" in times:
        if times["PM_OUT"] <= times["PM_IN"]:
            return False, "PM OUT must be after PM IN."

    return True, None


def calculate_hours_worked(record):
    try:
        am_in = parse_time_from_string(record.get("AM_IN", ""))
        am_out = parse_time_from_string(record.get("AM_OUT", ""))
        pm_in = parse_time_from_string(record.get("PM_IN", ""))
        pm_out = parse_time_from_string(record.get("PM_OUT", ""))

        if not all([am_in, am_out, pm_in, pm_out]):
            return None

        morning_hours = (am_out - am_in).total_seconds() / 3600
        afternoon_hours = (pm_out - pm_in).total_seconds() / 3600

        if morning_hours < 0 or afternoon_hours < 0:
            return None

        total_hours = morning_hours + afternoon_hours
        return round(total_hours, 2)
    except Exception as e:
        print(f"Hour calculation error: {e}")
        return None


def format_hours_display(hours):
    if hours is None:
        return None
    hours_int = int(hours)
    minutes = int(round((hours - hours_int) * 60))
    return f"{hours_int}h {minutes}m"


def get_today_records(name):
    try:
        all_records = sheet.get_all_records()
        today_str = today()

        user_records = []
        for record in all_records:
            timestamp = record.get("Timestamp", "")
            record_name = record.get("Name", "")
            if timestamp and record_name == name:
                try:
                    record_date = timestamp.split()[0]
                    if record_date == today_str:
                        user_records.append(record)
                except:
                    continue
        return user_records
    except Exception as e:
        print(f"Error getting today's records: {e}")
        return []


def get_full_record(name):
    record = {"AM_IN": "", "AM_OUT": "", "PM_IN": "", "PM_OUT": ""}
    today_records = get_today_records(name)

    for rec in today_records:
        time_clock = rec.get("Time Clock", "")
        input_time_sheet = rec.get("Input Time", "")

        input_time_discord = ""
        if input_time_sheet:
            try:
                t_dt = datetime.strptime(
                    input_time_sheet.strip(), "%I:%M:%S %p")
                hour_12 = t_dt.hour % 12
                if hour_12 == 0:
                    hour_12 = 12
                am_pm = "AM" if t_dt.hour < 12 else "PM"
                input_time_discord = f"{hour_12}:{t_dt.minute:02d} {am_pm}"
            except Exception:
                try:
                    t_dt2 = datetime.strptime(
                        input_time_sheet.strip(), "%I:%M %p")
                    hour_12 = t_dt2.hour % 12
                    if hour_12 == 0:
                        hour_12 = 12
                    am_pm = "AM" if t_dt2.hour < 12 else "PM"
                    input_time_discord = f"{hour_12}:{t_dt2.minute:02d} {am_pm}"
                except Exception:
                    input_time_discord = input_time_sheet

        if "AM - Time In" in time_clock:
            record["AM_IN"] = input_time_discord
        elif "AM - Time Out" in time_clock:
            record["AM_OUT"] = input_time_discord
        elif "PM - Time In" in time_clock:
            record["PM_IN"] = input_time_discord
        elif "PM - Time Out" in time_clock:
            record["PM_OUT"] = input_time_discord

    return record


def save_users():
    with open(USERS_FILE, "w") as f:
        json.dump(user_names, f, indent=4)


def is_admin(user_id):
    return user_id in ADMIN_IDS


def format_record_message(name, record):
    lines = [
        f"**{name}**",
        f"{pretty_date()}",
        ""
    ]
    if record["AM_IN"]:
        lines.append(f"AM IN: {record['AM_IN']}")
    if record["AM_OUT"]:
        lines.append(f"AM OUT: {record['AM_OUT']}")
    if record["PM_IN"]:
        lines.append(f"PM IN: {record['PM_IN']}")
    if record["PM_OUT"]:
        lines.append(f"PM OUT: {record['PM_OUT']}")
    return "\n".join(lines)

# ---------------- MESSAGES ---------------- #


MESSAGES_FILE = "messages.json"

if os.path.exists(MESSAGES_FILE):
    with open(MESSAGES_FILE, "r", encoding="utf-8") as f:
        messages = json.load(f)
else:
    messages = {"morning_person": [], "normal": [], "late": []}

# ---------------- ERROR HANDLER ---------------- #


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingRequiredArgument):
        command = ctx.command.name if ctx.command else "command"

        if command == "add_user":
            await ctx.send("❌ **Usage:** `!add_user @username Full Name`\n\nExample: `!add_user @john Juan Dela Cruz`")
        elif command == "change_name":
            await ctx.send("❌ **Usage:** `!change_name @username New Full Name`\n\nExample: `!change_name @john Juan Miguel Cruz`")
        elif command == "remove_user":
            await ctx.send("❌ **Usage:** `!remove_user @username`\n\nExample: `!remove_user @john`")
        elif command == "manual_entry":
            await ctx.send(
                "❌ **Usage:** `!manual_entry @username [am_in|am_out|pm_in|pm_out] [time]`\n\n"
                "**Examples:**\n"
                "`!manual_entry @john am_in 8:30 AM`\n"
                "`!manual_entry @maria pm_out 5:00 PM`\n"
                "`!manual_entry @pedro am_out 12:00 PM`"
            )
        elif command == "view_dtr":
            await ctx.send("❌ **Usage:** `!view_dtr @username`\n\nExample: `!view_dtr @john`")
        else:
            await ctx.send(f"❌ Oops! This command needs more information.\n\nTry `!help_dtr` to see how to use it.")

    elif isinstance(error, commands.CommandNotFound):
        pass

    else:
        await ctx.send(f"❌ Something went wrong. Please try again or contact an admin.")
        print(f"Error: {error}")

# ---------------- COMMANDS ---------------- #


@bot.event
async def on_ready():
    print(f"✅ {bot.user} is now online!")
    print(f"📊 Loaded {len(user_names)} authorized users")

# ---------------- ADMIN COMMANDS ---------------- #


@bot.command()
async def add_user(ctx, user_mention: discord.Member, *, full_name: str):
    """[ADMIN ONLY] Add a new user to the DTR system."""
    if not is_admin(ctx.author.id):
        await ctx.send("This command is only available to admins.")
        return

    uid = str(user_mention.id)

    if uid in user_names:
        await ctx.author.send(f"{user_mention.mention} is already registered as **{user_names[uid]}**")
        return

    formatted_name = format_name_with_initials(full_name.strip())
    user_names[uid] = full_name.strip()
    save_users()

    await ctx.author.send(
        f"Successfully added {user_mention.mention}:\n**{formatted_name}**\n"
        f"They can now use DTR commands!"
    )
    await ctx.send(f"{ctx.author.mention}, user added successfully")


@bot.command()
async def change_name(ctx, user_mention: discord.Member, *, new_name: str):
    """[ADMIN ONLY] Change a user's registered name."""
    if not is_admin(ctx.author.id):
        await ctx.send("This command is only available to admins.")
        return

    uid = str(user_mention.id)

    if uid not in user_names:
        await ctx.author.send(f"{user_mention.mention} is not in the system. Use !add_user first.")
        return

    old_name = format_name_with_initials(user_names[uid])
    formatted_name = format_name_with_initials(new_name.strip())

    user_names[uid] = new_name.strip()
    save_users()

    await ctx.author.send(
        f"Successfully updated name for {user_mention.mention}:\n"
        f"**{old_name}** → **{formatted_name}**"
    )
    await ctx.send(f"{ctx.author.mention}, name updated successfully")


@bot.command()
async def remove_user(ctx, user_mention: discord.Member):
    """[ADMIN ONLY] Remove a user from the DTR system."""
    if not is_admin(ctx.author.id):
        await ctx.send("This command is only available to admins.")
        return

    uid = str(user_mention.id)

    if uid not in user_names:
        await ctx.author.send(f"{user_mention.mention} is not in the system.")
        return

    removed_name = format_name_with_initials(user_names.pop(uid))
    save_users()

    await ctx.author.send(f"Successfully removed: **{removed_name}** ({user_mention.mention})")
    await ctx.send(f"{ctx.author.mention}, user removed successfully")


@bot.command()
async def list_users(ctx):
    """[ADMIN ONLY] List all authorized users."""
    if not is_admin(ctx.author.id):
        await ctx.send("This command is only available to admins.")
        return

    if not user_names:
        await ctx.author.send("No users in the system yet.")
        return

    user_list = []
    for uid, name in user_names.items():
        formatted_name = format_name_with_initials(name)
        if int(uid) in ADMIN_IDS:
            user_list.append(f"• {formatted_name} - ADMIN")
        else:
            user_list.append(f"• {formatted_name}")

    users_display = "\n".join(user_list)
    admin_count = sum(1 for uid in user_names.keys() if int(uid) in ADMIN_IDS)

    await ctx.author.send(
        f"**Authorized Users ({len(user_names)}):**\n"
        f"Admins: {admin_count} | Regular Users: {len(user_names) - admin_count}\n\n"
        f"{users_display}"
    )

    await ctx.send(f"{ctx.author.mention}, I sent you the user list in DM")


@bot.command()
async def manual_entry(ctx, user_mention: discord.Member, time_type: str, time_value: str):
    """[ADMIN ONLY] Manually add or correct a time entry for a user."""
    if not is_admin(ctx.author.id):
        await ctx.send("This command is only available to admins.")
        return

    uid = str(user_mention.id)
    if uid not in user_names:
        await ctx.author.send(f"{user_mention.mention} is not in the system. Use !add_user first.")
        return

    time_type = time_type.lower()
    valid_types = {"am_in": "AM - Time In", "am_out": "AM - Time Out",
                   "pm_in": "PM - Time In", "pm_out": "PM - Time Out"}

    if time_type not in valid_types:
        await ctx.author.send(
            "Invalid time type. Use one of: `am_in`, `am_out`, `pm_in`, `pm_out`\n"
            "Example: `!manual_entry @john pm_out 5:00 PM`"
        )
        return

    parsed_time = parse_time_from_string(time_value)
    if not parsed_time:
        await ctx.author.send(
            "Invalid time format. Use: `H:MM AM/PM`\nExamples: `8:30 AM`, `12:00 PM`, `5:45 PM`"
        )
        return

    hour_12 = parsed_time.hour % 12 or 12
    am_pm = "AM" if parsed_time.hour < 12 else "PM"
    time_sheet = f"{hour_12}:{parsed_time.minute:02d}:00 {am_pm}"

    name = format_name_with_initials(user_names[uid])

    record = get_full_record(name)
    test_record = record.copy()
    test_record[time_type.upper()] = time_sheet

    valid, error_msg = validate_time_sequence(test_record)
    if not valid:
        await ctx.author.send(f"Time Validation Warning: {error_msg}\nYou may still proceed.")

    timestamp = timestamp_str()
    try:
        sheet.append_row([timestamp, name, valid_types[time_type],
                         strip_leading_apostrophe(time_sheet)])
    except Exception as e:
        await ctx.author.send(f"Failed to add entry. Please try again or contact support.")
        print(f"Sheet error: {e}")
        return

    record = get_full_record(name)
    message = format_record_message(name, record)

    await ctx.author.send(
        f"**Manual Entry Added**\n"
        f"Admin: {ctx.author.mention}\n"
        f"User: {user_mention.mention}\n"
        f"Entry: {valid_types[time_type]} at {time_value}\n\n{message}"
    )
    await ctx.send(f"{ctx.author.mention}, manual entry added successfully")


@bot.command()
async def half_day(ctx, half: str = "morning"):
    """
    Record a half-day (morning or afternoon).
    Usage: !half_day morning OR !half_day afternoon
    """
    name = get_user_name(ctx)
    if not name:
        await ctx.send("You are not authorized to use the DTR system. Please contact an admin.")
        return

    half = half.lower()
    if half not in ["morning", "afternoon"]:
        await ctx.send(
            "Invalid option.\n"
            "Use `!half_day morning` or `!half_day afternoon`"
        )
        return

    record = get_full_record(name)
    timestamp = timestamp_str()

    if half == "morning":
        if not record["AM_IN"] or not record["AM_OUT"]:
            await ctx.send("You must clock **AM IN** and **AM OUT** first.")
            return

        if record["PM_IN"] or record["PM_OUT"]:
            await ctx.send("PM entries already exist. Cannot mark as morning half-day.")
            return

        try:
            sheet.append_row([timestamp, name, "PM - Time In", "N/A"])
            sheet.append_row([timestamp, name, "PM - Time Out", "N/A"])
        except Exception as e:
            await ctx.send("Failed to record half-day. Contact an admin.")
            print(f"Sheet error: {e}")
            return

        am_in = parse_time_from_string(record["AM_IN"])
        am_out = parse_time_from_string(record["AM_OUT"])

        message = format_record_message(name, record)
        message += "\n\n**Half-Day Recorded (Morning)**"

        if am_in and am_out:
            hours = (am_out - am_in).total_seconds() / 3600
            message += f"\nMorning Hours: {format_hours_display(hours)}"

        message += "\nPM Times: N/A"

        await ctx.send(message)
        return

    if record["AM_IN"] or record["AM_OUT"]:
        await ctx.send("AM entries already exist. Cannot mark as afternoon half-day.")
        return

    if record["PM_IN"] or record["PM_OUT"]:
        await ctx.send("PM entries already exist.")
        return

    try:
        sheet.append_row([timestamp, name, "AM - Time In", "N/A"])
        sheet.append_row([timestamp, name, "AM - Time Out", "N/A"])
    except Exception as e:
        await ctx.send("Failed to record half-day. Contact an admin.")
        print(f"Sheet error: {e}")
        return

    await ctx.send(
        "**Afternoon Half-Day Set**\n\n"
        "You may now clock:\n"
        "• `!pm_in`\n"
        "• `!pm_out`"
    )


@bot.command()
async def view_dtr(ctx, user_mention: discord.Member = None):
    """[ADMIN ONLY] View DTR record for any user today."""
    if not is_admin(ctx.author.id):
        await ctx.send("This command is only available to admins.")
        return

    if not user_mention:
        await ctx.author.send("Usage: `!view_dtr @username`")
        return

    uid = str(user_mention.id)
    if uid not in user_names:
        await ctx.author.send(f"{user_mention.mention} is not in the system.")
        return

    name = format_name_with_initials(user_names[uid])
    record = get_full_record(name)

    if not any(record.values()):
        await ctx.author.send(f"**{name}** has no DTR record for today yet.")
        return

    message = format_record_message(name, record)

    if all([record.get("AM_IN"), record.get("AM_OUT"), record.get("PM_IN"), record.get("PM_OUT")]):
        if record.get("PM_IN") == "N/A" or record.get("PM_OUT") == "N/A":
            am_in = parse_time_from_string(record.get("AM_IN", ""))
            am_out = parse_time_from_string(record.get("AM_OUT", ""))
            if am_in and am_out:
                morning_hours = (am_out - am_in).total_seconds() / 3600
                hours_display = format_hours_display(morning_hours)
                message += f"\n\n**Half-Day**\nMorning Hours: {hours_display}"
        else:
            hours_worked = calculate_hours_worked(record)
            if hours_worked:
                hours_display = format_hours_display(hours_worked)
                message += f"\n\n**Total Hours Worked: {hours_display}"
                if hours_worked >= REQUIRED_HOURS:
                    message += f"\n**Completed {REQUIRED_HOURS}-hour requirement!**"
                else:
                    shortage = REQUIRED_HOURS - hours_worked
                    shortage_display = format_hours_display(shortage)
                    message += f"\n**Undertime: {shortage_display}**"

    await ctx.author.send(f"**DTR Record for {user_mention.mention}**\n\n{message}")
    await ctx.send(f"{ctx.author.mention}, DTR record sent to your DM")


@bot.command()
async def users(ctx):
    """List all users in the DTR system (anyone can use this)."""
    if not user_names:
        await ctx.send("No users in the system yet.")
        return

    sorted_users = sorted(
        [(format_name_with_initials(name), name)
         for name in user_names.values()],
        key=lambda x: x[0]
    )

    user_list = "\n".join(
        [f"• {formatted_name}" for formatted_name, _ in sorted_users])
    await ctx.send(f"**DTR System Users ({len(user_names)}):**\n\n{user_list}")

# ---------------- Clock commands ---------------- #


@bot.command()
async def am_in(ctx):
    """Clock in for the morning shift."""
    name = get_user_name(ctx)
    if not name:
        await ctx.send("You are not authorized to use the DTR system. Please contact an admin.")
        return

    record = get_full_record(name)
    if record["AM_IN"]:
        await ctx.send("You already clocked AM IN today.")
        return

    timestamp = timestamp_str()
    time_sheet = time_for_sheets()
    time_discord = time_for_discord()
    late_status = is_late()

    try:
        sheet.append_row([timestamp, name, "AM - Time In",
                         strip_leading_apostrophe(time_sheet)])
    except Exception as e:
        await ctx.send(f"Failed to record AM IN. Please try again or contact admin.")
        print(f"Sheet error: {e}")
        return

    record = get_full_record(name)
    message = format_record_message(name, record)

    current_time = (now().hour, now().minute)

    if current_time < MORNING_PERSON_CUTOFF and messages["morning_person"]:
        message += "\n" + random.choice(messages["morning_person"])
    elif late_status and messages["late"]:
        message += "\n" + random.choice(messages["late"])
    elif messages["normal"]:
        message += "\n" + random.choice(messages["normal"])

    await ctx.send(message)


@bot.command()
async def am_out(ctx):
    """Clock out for lunch break."""
    name = get_user_name(ctx)
    if not name:
        await ctx.send("You are not authorized to use the DTR system. Please contact an admin.")
        return

    record = get_full_record(name)

    if not record["AM_IN"]:
        await ctx.send("You must clock AM IN first.")
        return

    if record["AM_OUT"]:
        await ctx.send("You already clocked AM OUT today.")
        return

    if record["PM_OUT"]:
        await ctx.send("Your work day is already complete. You cannot modify times after PM OUT.")
        return

    timestamp = timestamp_str()
    time_sheet = time_for_sheets()

    test_record = record.copy()
    test_record["AM_OUT"] = time_sheet
    valid, error_msg = validate_time_sequence(test_record)

    if not valid:
        await ctx.send(f"{error_msg}")
        return

    try:
        sheet.append_row([timestamp, name, "AM - Time Out",
                         strip_leading_apostrophe(time_sheet)])
    except Exception as e:
        await ctx.send(f"Failed to record AM OUT. Please try again or contact admin.")
        print(f"Sheet error: {e}")
        return

    record = get_full_record(name)
    message = format_record_message(name, record)
    await ctx.send(message)


@bot.command()
async def pm_in(ctx):
    """Clock in after lunch break."""
    name = get_user_name(ctx)
    if not name:
        await ctx.send("You are not authorized to use the DTR system. Please contact an admin.")
        return

    record = get_full_record(name)

    if not record["AM_OUT"]:
        await ctx.send("You must clock AM OUT first.")
        return

    if record["PM_IN"]:
        await ctx.send("You already clocked PM IN today.")
        return

    if record["PM_OUT"]:
        await ctx.send("Your work day is already complete. You cannot modify times after PM OUT.")
        return

    timestamp = timestamp_str()
    time_sheet = time_for_sheets()

    test_record = record.copy()
    test_record["PM_IN"] = time_sheet
    valid, error_msg = validate_time_sequence(test_record)

    if not valid:
        await ctx.send(f"{error_msg}")
        return

    try:
        sheet.append_row([timestamp, name, "PM - Time In",
                         strip_leading_apostrophe(time_sheet)])
    except Exception as e:
        await ctx.send(f"Failed to record PM IN. Please try again or contact admin.")
        print(f"Sheet error: {e}")
        return

    record = get_full_record(name)
    message = format_record_message(name, record)
    await ctx.send(message)


@bot.command()
async def pm_out(ctx):
    """Clock out at end of workday."""
    name = get_user_name(ctx)
    if not name:
        await ctx.send("You are not authorized to use the DTR system. Please contact an admin.")
        return

    record = get_full_record(name)

    if not record["PM_IN"]:
        await ctx.send("You must clock PM IN first.")
        return

    if record["PM_OUT"]:
        await ctx.send("Your work day is already complete. You cannot clock out again.")
        return

    timestamp = timestamp_str()
    time_sheet = time_for_sheets()

    test_record = record.copy()
    test_record["PM_OUT"] = time_sheet
    valid, error_msg = validate_time_sequence(test_record)

    if not valid:
        await ctx.send(f"Time validation warning: {error_msg}")

    try:
        sheet.append_row([timestamp, name, "PM - Time Out",
                         strip_leading_apostrophe(time_sheet)])
    except Exception as e:
        await ctx.send(f"Failed to record PM OUT. Please try again or contact admin.")
        print(f"Sheet error: {e}")
        return

    record = get_full_record(name)
    message = format_record_message(name, record)

    hours_worked = calculate_hours_worked(record)
    if hours_worked:
        hours_display = format_hours_display(hours_worked)
        message += f"\n\n**Total Hours Worked: {hours_display}**"

        if hours_worked >= REQUIRED_HOURS:
            message += f"\n**Completed {REQUIRED_HOURS}-hour requirement!**"
        else:
            shortage = REQUIRED_HOURS - hours_worked
            shortage_display = format_hours_display(shortage)
            message += f"\n**Undertime: {shortage_display}**"
    else:
        message += "\n\n**Complete DTR for today!**"

    await ctx.send(message)

# ---------------- Status & Help ---------------- #


@bot.command()
async def status(ctx):
    """Check your current DTR status for today."""
    name = get_user_name(ctx)
    if not name:
        await ctx.send("You are not authorized to use the DTR system. Please contact an admin.")
        return

    record = get_full_record(name)

    if not any(record.values()):
        await ctx.send(f"No DTR record for today yet.\n\nUse !am_in to start your day!")
        return

    message = format_record_message(name, record)

    if all([record.get("AM_IN"), record.get("AM_OUT"), record.get("PM_IN"), record.get("PM_OUT")]):
        hours_worked = calculate_hours_worked(record)
        if hours_worked:
            hours_display = format_hours_display(hours_worked)
            message += f"\n\n**Total Hours Worked: {hours_display}**"

            if hours_worked >= REQUIRED_HOURS:
                message += f"\n**Completed {REQUIRED_HOURS}-hour requirement!**"
            else:
                shortage = REQUIRED_HOURS - hours_worked
                shortage_display = format_hours_display(shortage)
                message += f"\n**Undertime: {shortage_display}**"

    await ctx.send(message)


@bot.command()
async def help_dtr(ctx):
    """Show DTR bot commands and usage."""
    is_user_admin = is_admin(ctx.author.id)

    help_text = """
**🕒 DTR HAWKS Bot Commands**

━━━━━━━━━━━━━━━━━━
**Daily Time Recording**
━━━━━━━━━━━━━━━━━━
!am_in  - Clock in (morning)
!am_out - Clock out (lunch break)
!pm_in  - Clock in (afternoon)
!pm_out - Clock out (end of day)

━━━━━━━━━━━━━━━━━━
**Half-Day Options**
━━━━━━━━━━━━━━━━━━
!half_day morning
• Use AFTER you finish morning work
• Required: AM IN → AM OUT
• PM times will be marked as N/A

!half_day afternoon
• Use BEFORE starting afternoon work
• AM times will be marked as N/A
• Then clock: PM IN → PM OUT

━━━━━━━━━━━━━━━━━━
**Info Commands**
━━━━━━━━━━━━━━━━━━
!status    - View your DTR for today
!help_dtr  - Show this help message

━━━━━━━━━━━━━━━━━━
**Important Notes**
━━━━━━━━━━━━━━━━━━
• Only authorized users can use DTR commands
• Follow the full-day sequence:
  AM IN → AM OUT → PM IN → PM OUT
• Morning half-day = confirm AFTER AM OUT
• Afternoon half-day = declare BEFORE PM IN
• Late threshold: 10:00 AM
"""

    if is_user_admin:
        help_text += """
━━━━━━━━━━━━━━━━━━
**🔐 Admin Commands**
━━━━━━━━━━━━━━━━━━
!add_user @user Full Name
!change_name @user New Name
!remove_user @user
!list_users
!view_dtr @user
!manual_entry @user [am_in | am_out | pm_in | pm_out] [time]

━━━━━━━━━━━━━━━━━━
**Manual Entry Examples**
━━━━━━━━━━━━━━━━━━
!manual_entry @john am_in 8:30 AM
!manual_entry @maria pm_out 5:00 PM
!manual_entry @pedro am_out 12:00 PM

━━━━━━━━━━━━━━━━━━
**Admin Use Cases**
━━━━━━━━━━━━━━━━━━
• User forgot to clock → add missing time
• Wrong time recorded → correct entry
• Fix time sequence issues
"""
    await ctx.send(help_text)

# ---------------- SIMPLE STARTUP ---------------- #

if __name__ == "__main__":
    print("🚀 Starting DTR HAWKS Bot...")
    print(f"📡 PORT: {PORT}")
    print(f"🌍 Timezone: {TIMEZONE}")

    # Start Flask in background
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    print(f"✅ Flask server running on port {PORT}")

    # Start Discord bot - simple, no retries
    try:
        bot.run(DISCORD_TOKEN)
    except Exception as e:
        print(f"❌ Bot failed to start: {e}")
