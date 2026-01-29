import discord
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import pytz
import json
import os
import random
import webserver

# ---------------- CONFIG ---------------- #
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise ValueError("DISCORD_TOKEN not set")

TIMEZONE = pytz.timezone("Asia/Manila")
AM_IN_CUTOFF = (10, 0)            # 10:00 AM - late threshold (hour, minute)
REQUIRED_HOURS = 8                # Required work hours per day
ADMIN_IDS = [1429858548392792288] # Add Discord user IDs of admins here (ints)
MORNING_PERSON_CUTOFF = (7, 44)   # Anyone before this is a morning person
# ---------------------------------------- #

# --- Google Sheets Setup ---
# Uses a service account credential JSON file named "credentials.json"
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open("DTR HAWKS").sheet1
# --------------------------------

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
# --------------------------------

# ---------------- STORAGE ---------------- #
USERS_FILE = "users.json"

if os.path.exists(USERS_FILE):
    with open(USERS_FILE, "r") as f:
        user_names = json.load(f)
else:
    user_names = {}
# No temporary in-memory DTR storage — everything is appended to Google Sheets.
# ----------------------------------------

# ---------------- HELPERS ---------------- #

def now():
    """Return timezone-aware 'now' in configured TIMEZONE."""
    return datetime.now(TIMEZONE)

def today():
    """Return today's date string used by your sheet records: M/D/YYYY (no leading zeros)."""
    n = now()
    return f"{n.month}/{n.day}/{n.year}"

def pretty_date():
    """Human friendly date for Discord messages, e.g. 'June 27, 2024'."""
    return now().strftime("%B %d, %Y")

def timestamp_str():
    """Full timestamp used for the sheet Timestamp column:
       Format: M/D/YYYY H:MM:SS  (no padded month/day)"""
    """n = now()
    return f"{n.month}/{n.day}/{n.year} {n.hour}:{n.minute:02d}:{n.second:02d}"""

    n = now()
    hour_12 = n.hour % 12
    if hour_12 == 0:
        hour_12 = 12
    am_pm = "AM" if n.hour < 12 else "PM"
    return f"{n.month}/{n.day}/{n.year} {hour_12}:{n.minute:02d}:{n.second:02d} {am_pm}"

# --- Time formatting helpers (SHEET vs DISCORD) ---
def time_for_sheets():
    """Return time string for Google Sheets: H:MM:00 AM/PM (seconds forced to 00)."""
    n = now()
    hour_12 = n.hour % 12
    if hour_12 == 0:
        hour_12 = 12
    am_pm = "AM" if n.hour < 12 else "PM"
    return f"{hour_12}:{n.minute:02d}:00 {am_pm}"  # seconds always 00 for sheet

def time_for_discord():
    """Return time string for Discord messages: H:MM AM/PM (no seconds, no leading zero hour)."""
    n = now()
    hour_12 = n.hour % 12
    if hour_12 == 0:
        hour_12 = 12
    am_pm = "AM" if n.hour < 12 else "PM"
    return f"{hour_12}:{n.minute:02d} {am_pm}"

# --- Name formatting utilities ---
def format_name_with_initials(full_name):
    """
    Convert name to format: First MiddleInitial. Last
    Examples:
      "Juan Dela Cruz" -> "Juan D. Cruz"
      "John Paul Santos" -> "John Paul C. Santos"
    If only two parts are provided, a default middle initial 'C.' is inserted.
    """
    parts = full_name.strip().split()
    if len(parts) < 2:
        return full_name  # not enough parts to format

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
    """Return the formatted name registered for the Discord user, or None."""
    uid = str(ctx.author.id)
    return user_names.get(uid, None)

def is_late():
    """Return True if current time is at-or-after AM_IN_CUTOFF."""
    late_time = now().replace(hour=AM_IN_CUTOFF[0], minute=AM_IN_CUTOFF[1],
                               second=0, microsecond=0)
    return now() >= late_time

# --- Time parsing & validation ---
def parse_time_from_string(time_string):
    """
    Parse time strings to a datetime object (today's date).
    Accepts formats:
      - "H:MM:SS AM/PM"  (sheet format)
      - "HH:MM:SS"       (24-hour, optional)
      - "H:MM AM/PM"     (discord format)
    Returns a naive datetime (date is today) or None if parsing fails.
    """
    if not time_string:
        return None
    try:
        time_string = time_string.strip()
        formats = [
            "%I:%M:%S %p",  # e.g., "7:00:00 AM"
            "%H:%M:%S",     # e.g., "19:00:00"
            "%I:%M %p",     # e.g., "7:00 AM"
        ]
        for fmt in formats:
            try:
                t = datetime.strptime(time_string, fmt).time()
                # combine with today's date (naive) for arithmetic/comparisons
                return datetime.combine(now().date(), t)
            except ValueError:
                continue
        return None
    except Exception as e:
        print(f"Error parsing time '{time_string}': {e}")
        return None

def validate_time_sequence(record):
    """
    Ensure logical order:
      AM_IN < AM_OUT < PM_IN < PM_OUT
    Accepts times either in sheet format (with seconds) or discord format (without).
    Returns (True, None) if OK else (False, "error message").
    """
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

# --- Hours calculation ---
def calculate_hours_worked(record):
    """
    Calculate total worked hours from AM_IN->AM_OUT and PM_IN->PM_OUT.
    record values are expected in a parseable format (sheet or discord).
    Returns total hours as float rounded to 2 decimals, or None if incomplete/invalid.
    """
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
    """Format decimal hours (e.g., 7.5) as '7h 30m'."""
    if hours is None:
        return None
    hours_int = int(hours)
    minutes = int(round((hours - hours_int) * 60))
    return f"{hours_int}h {minutes}m"

# --- Google Sheets readers ---
def get_today_records(name):
    """
    Retrieve all rows from the sheet for 'name' that match today's date.
    Expects the sheet headers to include: 'Timestamp', 'Name', 'Time Clock', 'Input Time'
    """
    try:
        all_records = sheet.get_all_records()
        today_str = today()

        user_records = []
        for record in all_records:
            timestamp = record.get("Timestamp", "")
            record_name = record.get("Name", "")
            if timestamp and record_name == name:
                try:
                    record_date = timestamp.split()[0]  # date part of "M/D/YYYY H:MM:SS"
                    if record_date == today_str:
                        user_records.append(record)
                except:
                    continue
        return user_records
    except Exception as e:
        print(f"Error getting today's records: {e}")
        return []

def get_full_record(name):
    """
    Build a dictionary for today's AM/PM times for 'name' using Google Sheets Input Time.
    This function converts the sheet's 'Input Time' (H:MM:00 AM/PM) to a Discord-friendly
    format (H:MM AM/PM) for display.
    Returns: {"AM_IN": "...", "AM_OUT": "...", "PM_IN": "...", "PM_OUT": "..."}
    """
    record = {"AM_IN": "", "AM_OUT": "", "PM_IN": "", "PM_OUT": ""}
    today_records = get_today_records(name)

    for rec in today_records:
        time_clock = rec.get("Time Clock", "")
        input_time_sheet = rec.get("Input Time", "")  # sheet format expected H:MM:00 AM/PM

        # Convert sheet time to Discord-friendly format (H:MM AM/PM)
        input_time_discord = ""
        if input_time_sheet:
            try:
                # Try parsing the sheet format first
                t_dt = datetime.strptime(input_time_sheet.strip(), "%I:%M:%S %p")
                # Manual formatting to avoid platform-specific %-I issues:
                hour_12 = t_dt.hour % 12
                if hour_12 == 0:
                    hour_12 = 12
                am_pm = "AM" if t_dt.hour < 12 else "PM"
                input_time_discord = f"{hour_12}:{t_dt.minute:02d} {am_pm}"
            except Exception:
                # If parsing fails, fall back to the raw sheet value or attempt to parse discord format:
                try:
                    t_dt2 = datetime.strptime(input_time_sheet.strip(), "%I:%M %p")
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

# --- Persistence helpers ---
def save_users():
    with open(USERS_FILE, "w") as f:
        json.dump(user_names, f, indent=4)

def is_admin(user_id):
    """Check if a numeric Discord user_id is in ADMIN_IDS."""
    return user_id in ADMIN_IDS

# --- Name validation ---
def validate_name(name):
    """Simple checks for name validity."""
    if not name or len(name.strip()) < 3:
        return False, "Name must be at least 3 characters long."
    if len(name) > 100:
        return False, "Name is too long (max 100 characters)."
    if not all(c.isalpha() or c.isspace() or c in ".-'" for c in name):
        return False, "Name can only contain letters, spaces, hyphens, apostrophes, and periods."
    parts = name.strip().split()
    if len(parts) < 2:
        return False, "Please provide at least a first name and last name."
    return True, None

# --- Discord message formatter ---
def format_record_message(name, record):
    """
    Format the DTR summary that will be sent to Discord.
    record values should already be in Discord-friendly format (H:MM AM/PM).
    """
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
    """Basic command error handling."""
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing required argument: {error.param.name}")
    elif isinstance(error, commands.CommandNotFound):
        # ignore unknown commands silently
        pass
    else:
        await ctx.send(f"An error occurred: {str(error)}")
        print(f"Error: {error}")

# ---------------- COMMANDS ---------------- #

@bot.event
async def on_ready():
    print(f"{bot.user} is now online!")
    print(f"Loaded {len(user_names)} registered users")

@bot.command()
async def register(ctx, *, full_name: str):
    """Register your full name for DTR tracking. (One-time only)"""
    uid = str(ctx.author.id)

    if uid in user_names:
        await ctx.send(
            f"You are already registered as **{user_names[uid]}**.\n\n"
            f"Only admins can change registered names. Please contact an admin if you need to update your name."
        )
        return

    valid, error_msg = validate_name(full_name)
    if not valid:
        await ctx.send(f"{error_msg}")
        return

    formatted_name = format_name_with_initials(full_name.strip())
    user_names[uid] = formatted_name
    save_users()

    await ctx.send(
        f"Successfully registered:\n**{formatted_name}**\n\n"
        f"You can now use DTR commands!"
    )

@bot.command()
async def change_name(ctx, user_mention: discord.Member, *, new_name: str):
    """[ADMIN ONLY] Change a user's registered name."""
    if not is_admin(ctx.author.id):
        await ctx.send("This command is only available to admins.")
        return

    valid, error_msg = validate_name(new_name)
    if not valid:
        await ctx.send(f"{error_msg}")
        return

    uid = str(user_mention.id)
    old_name = user_names.get(uid, "Not registered")

    formatted_name = format_name_with_initials(new_name.strip())
    user_names[uid] = formatted_name
    save_users()

    await ctx.send(
        f"Successfully updated name for {user_mention.mention}:\n"
        f"**{old_name}** → **{formatted_name}**"
    )

@bot.command()
async def unregister(ctx, user_mention: discord.Member):
    """[ADMIN ONLY] Remove a user's registration."""
    if not is_admin(ctx.author.id):
        await ctx.send("This command is only available to admins.")
        return

    uid = str(user_mention.id)

    if uid not in user_names:
        await ctx.send(f"{user_mention.mention} is not registered.")
        return

    removed_name = user_names.pop(uid)
    save_users()

    await ctx.send(f"Successfully unregistered: **{removed_name}** ({user_mention.mention})")

@bot.command()
async def list_users(ctx):
    """[ADMIN ONLY] List all registered users."""
    if not is_admin(ctx.author.id):
        await ctx.send("This command is only available to admins.")
        return

    if not user_names:
        await ctx.send("No users registered yet.")
        return

    user_list = "\n".join([f"• {name} (ID: {uid})" for uid, name in user_names.items()])
    await ctx.send(f"**Registered Users ({len(user_names)}):**\n\n{user_list}")

# ---------------- Clock commands ---------------- #

@bot.command()
async def am_in(ctx):
    """Clock in for the morning shift."""
    name = get_user_name(ctx)
    if not name:
        await ctx.send("Please register first using !register FirstName MiddleName LastName")
        return

    record = get_full_record(name)
    if record["AM_IN"]:
        await ctx.send("You already clocked AM IN today.")
        return

    timestamp = timestamp_str()
    time_sheet = time_for_sheets()       # for Google Sheets
    time_discord = time_for_discord()     # for Discord messages
    late_status = is_late()
    
    try:
        sheet.append_row([timestamp, name, "AM - Time In", time_sheet])
    except Exception as e:
        await ctx.send(f"Failed to record AM IN. Please try again or contact admin.")
        print(f"Sheet error: {e}")
        return

    record = get_full_record(name)
    message = format_record_message(name, record)
    
    current_hour = now().hour

    # Morning person
    if current_hour < MORNING_PERSON_CUTOFF and messages["morning_person"]:
        message += "\n" + random.choice(messages["morning_person"])
    # Late
    elif late_status and messages["late"]:
        message += "\n" + random.choice(messages["late"])
    # Normal
    elif messages["normal"]:
        message += "\n" + random.choice(messages["normal"])

    await ctx.send(message)

@bot.command()
async def am_out(ctx):
    """Clock out for lunch break."""
    name = get_user_name(ctx)
    if not name:
        await ctx.send("Please register first using !register FirstName MiddleName LastName")
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

    # Validate time sequence using a test record (mix of discord-formatted existing values and sheet format for the new value)
    test_record = record.copy()
    test_record["AM_OUT"] = time_sheet
    valid, error_msg = validate_time_sequence(test_record)

    if not valid:
        await ctx.send(f"{error_msg}")
        return

    try:
        sheet.append_row([timestamp, name, "AM - Time Out", time_sheet])
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
        await ctx.send("Please register first using !register FirstName MiddleName LastName")
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
        await ctx.send(f"❌ {error_msg}")
        return

    try:
        sheet.append_row([timestamp, name, "PM - Time In", time_sheet])
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
        await ctx.send("Please register first using !register FirstName MiddleName LastName")
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

    # Validate time sequence
    test_record = record.copy()
    test_record["PM_OUT"] = time_sheet
    valid, error_msg = validate_time_sequence(test_record)

    if not valid:
        # we allow continuing but warn the user
        await ctx.send(f"Time validation warning: {error_msg}")

    try:
        sheet.append_row([timestamp, name, "PM - Time Out", time_sheet])
    except Exception as e:
        await ctx.send(f"Failed to record PM OUT. Please try again or contact admin.")
        print(f"Sheet error: {e}")
        return

    record = get_full_record(name)
    message = format_record_message(name, record)

    # Calculate total hours worked (record values are discord-formatted but parseable)
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
        await ctx.send("Please register first using !register FirstName MiddleName LastName")
        return

    record = get_full_record(name)

    if not any(record.values()):
        await ctx.send(f"No DTR record for today yet.\n\nUse !am_in to start your day!")
        return

    message = format_record_message(name, record)

    # If all times complete, show hours worked
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
**DTR HAWKS Bot Commands**

**Setup:**
!register FirstName MiddleName LastName - Register your name (one-time only)

**Daily Time Recording:**
!am_in - Clock in (morning)
!am_out - Clock out (lunch break)
!pm_in - Clock in (after lunch)
!pm_out - Clock out (end of day)

**Info:**
!status - Check your DTR for today
!help_dtr - Show this help message

**Notes:**
• You must register before using DTR commands
• Names are automatically formatted with middle initial (e.g., "Juan D. Cruz")
• You can only register once - contact admin to change name
• Follow the sequence: AM IN → AM OUT → PM IN → PM OUT
• Late threshold: 10:00 AM
• Required work hours: 8 hours
• Times are automatically validated
• Once PM OUT is recorded, the day is complete and cannot be modified
    """

    if is_user_admin:
        help_text += """
**Admin Commands:**
!change_name @user NewName - Change a user's registered name
!unregister @user - Remove user registration
!list_users - List all registered users
        """

    await ctx.send(help_text)

# ---------------- RUN ---------------- #

webserver.keep_alive()
bot.run(DISCORD_TOKEN)
