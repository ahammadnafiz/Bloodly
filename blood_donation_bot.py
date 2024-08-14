import os
import logging
from datetime import datetime
import re
from typing import List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ConversationHandler, CallbackContext
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import pandas as pd
import asyncpg
from dotenv import load_dotenv
from keep_alive import keep_alive
import asyncio

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv('.env')
BOT_TOKEN = os.getenv('API')
DATABASE_URL = os.getenv('DATABASE_URL')  # PostgreSQL connection string

# Define constants
MENU, LOCATION, BLOOD_TYPE, CONTACT, PROFILE, FIND, EMERGENCY, LOCATION_FIND = range(8)
BLOOD_TYPES = ['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-']

# Initialize the geolocator
geolocator = Nominatim(user_agent="blood_donation_bot")

async def setup_database():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS donors (
            id SERIAL PRIMARY KEY,
            user_id BIGINT UNIQUE,
            name TEXT NOT NULL,
            blood_type TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            contact TEXT NOT NULL,
            last_donation DATE
        )
        """)
        await conn.close()
    except Exception as e:
        logger.error(f"Database setup error: {e}")

def parse_dms_coordinate(coord_str):
    pattern = r"(\d+)°(\d+)'([\d.]+)\"([NS])\s*(\d+)°(\d+)'([\d.]+)\"([EW])"
    match = re.match(pattern, coord_str)
    if not match:
        return None
    
    lat_d, lat_m, lat_s, lat_dir, lon_d, lon_m, lon_s, lon_dir = match.groups()
    
    lat = (float(lat_d) + float(lat_m)/60 + float(lat_s)/3600) * (1 if lat_dir == 'N' else -1)
    lon = (float(lon_d) + float(lon_m)/60 + float(lon_s)/3600) * (1 if lon_dir == 'E' else -1)
    
    return lat, lon

async def start(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    conn = await asyncpg.connect(DATABASE_URL)
    user_is_registered = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM donors WHERE user_id = $1)", user_id)
    await conn.close()

    keyboard = [
        [InlineKeyboardButton("Donate Blood 🩸", callback_data='donate'),
         InlineKeyboardButton("Find Blood 🔍", callback_data='find')],
        [InlineKeyboardButton("Emergency Request 🚨", callback_data='emergency')]
    ]
    if user_is_registered:
        keyboard.append([InlineKeyboardButton("My Profile 👤", callback_data='profile')])

    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        'Welcome to the Bangladesh Blood Donation Bot! 🇧🇩\n\n'
        'This bot helps connect blood donors with those in need. '
        'What would you like to do?',
        reply_markup=reply_markup
    )
    return MENU

async def menu_callback(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    
    if query.data == 'donate':
        await query.message.reply_text('Please share your location. You can send your current location, a Google Maps link, or type an address.')
        return LOCATION
    elif query.data == 'find':
        return await find_blood(update, context)
    elif query.data == 'emergency':
        keyboard = [[InlineKeyboardButton(bt, callback_data=f'blood_{bt}') for bt in BLOOD_TYPES[i:i+2]] for i in range(0, len(BLOOD_TYPES), 2)]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text('What blood type do you need?', reply_markup=reply_markup)
        return EMERGENCY
    elif query.data == 'profile':
        return await show_profile(update, context)
    else:
        await query.message.reply_text('Invalid choice. Please select from the options provided.')
        return MENU

async def location(update: Update, context: CallbackContext) -> int:
    user = update.message.from_user
    
    if update.message.location:
        context.user_data['latitude'] = update.message.location.latitude
        context.user_data['longitude'] = update.message.location.longitude
    elif update.message.text:
        coords = parse_dms_coordinate(update.message.text)
        if coords:
            context.user_data['latitude'], context.user_data['longitude'] = coords
        else:
            coords = extract_coords_from_google_maps_link(update.message.text)
            if coords:
                context.user_data['latitude'], context.user_data['longitude'] = coords
            else:
                try:
                    location = geolocator.geocode(f"{update.message.text}, Bangladesh")
                    if location:
                        context.user_data['latitude'] = location.latitude
                        context.user_data['longitude'] = location.longitude
                    else:
                        await update.message.reply_text('Invalid location. Please send a Google Maps link, your current location, or a specific address.')
                        return LOCATION
                except Exception as e:
                    logger.error(f"Geocoding error: {e}")
                    await update.message.reply_text('An error occurred while processing your location. Please try again.')
                    return LOCATION
    else:
        await update.message.reply_text('Invalid location. Please send a Google Maps link, your current location, or a specific address.')
        return LOCATION

    keyboard = [[InlineKeyboardButton(bt, callback_data=f'blood_{bt}') for bt in BLOOD_TYPES[i:i+2]] for i in range(0, len(BLOOD_TYPES), 2)]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text('What is your blood type?', reply_markup=reply_markup)
    return BLOOD_TYPE

def extract_coords_from_google_maps_link(link):
    patterns = [
        r"@(-?\d+\.\d+),(-?\d+\.\d+)",
        r"ll=(-?\d+\.\d+),(-?\d+\.\d+)"
    ]
    for pattern in patterns:
        match = re.search(pattern, link)
        if match:
            return float(match.group(1)), float(match.group(2))
    return None

async def blood_type_callback(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    
    blood_type = query.data.split('_')[1]
    context.user_data['blood_type'] = blood_type
    await query.message.reply_text('Please provide your contact number.')
    return CONTACT

async def contact(update: Update, context: CallbackContext) -> int:
    contact = update.message.text
    
    if re.match(r'^\+?880\d{10}$', contact):
        context.user_data['contact'] = contact
        await update.message.reply_text('When was your last blood donation? (YYYY-MM-DD or "Never")')
        return PROFILE
    else:
        await update.message.reply_text('Invalid contact number. Please enter a valid Bangladesh phone number.')
        return CONTACT

async def profile(update: Update, context: CallbackContext) -> int:
    last_donation = update.message.text
    
    if last_donation.lower() == 'never':
        last_donation = None
    else:
        try:
            last_donation = datetime.strptime(last_donation, '%Y-%m-%d').date().isoformat()
        except ValueError:
            await update.message.reply_text('Invalid date format. Please use YYYY-MM-DD or "Never".')
            return PROFILE

    user_id = update.effective_user.id
    name = update.effective_user.full_name
    
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute("""
        INSERT INTO donors 
        (user_id, name, blood_type, latitude, longitude, contact, last_donation) 
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (user_id) DO UPDATE 
        SET name = EXCLUDED.name, 
            blood_type = EXCLUDED.blood_type, 
            latitude = EXCLUDED.latitude, 
            longitude = EXCLUDED.longitude, 
            contact = EXCLUDED.contact, 
            last_donation = EXCLUDED.last_donation
        """, user_id, name, context.user_data['blood_type'], 
              context.user_data['latitude'], context.user_data['longitude'], 
              context.user_data['contact'], last_donation)
        await conn.close()
        
        await update.message.reply_text('Thank you for registering as a donor! 🎉\n\nYour information has been saved successfully.')
    except Exception as e:
        logger.error(f"Database error: {e}")
        await update.message.reply_text('An error occurred while saving your information. Please try again later.')
    
    return ConversationHandler.END

async def find_nearest_donors(lat: float, lon: float, blood_type: str, limit: int = 5) -> List[Tuple]:
    logger.info(f"Finding nearest donors to ({lat}, {lon}) with blood type {blood_type}")
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        rows = await conn.fetch("""
        SELECT name, contact, latitude, longitude, 
        geodesic((latitude, longitude), ($1, $2)) as distance
        FROM donors
        WHERE blood_type = $3
        ORDER BY distance ASC
        LIMIT $4
        """, lat, lon, blood_type, limit)
        await conn.close()
        return [(row['name'], row['contact'], row['distance']) for row in rows]
    except Exception as e:
        logger.error(f"Error finding nearest donors: {e}")
        return []

async def find_blood(update: Update, context: CallbackContext) -> int:
    await update.callback_query.answer()
    await update.callback_query.message.reply_text('Please share your location to find donors nearby.')
    return LOCATION_FIND

async def emergency_callback(update: Update, context: CallbackContext) -> int:
    query = update.callback_query
    await query.answer()
    
    blood_type = query.data.split('_')[1]
    context.user_data['blood_type'] = blood_type
    
    await query.message.reply_text('Please share your location for the emergency request.')
    return LOCATION_FIND

async def location_find(update: Update, context: CallbackContext) -> int:
    user = update.message.from_user
    
    if update.message.location:
        latitude = update.message.location.latitude
        longitude = update.message.location.longitude
    elif update.message.text:
        coords = parse_dms_coordinate(update.message.text)
        if coords:
            latitude, longitude = coords
        else:
            coords = extract_coords_from_google_maps_link(update.message.text)
            if coords:
                latitude, longitude = coords
            else:
                try:
                    location = geolocator.geocode(f"{update.message.text}, Bangladesh")
                    if location:
                        latitude = location.latitude
                        longitude = location.longitude
                    else:
                        await update.message.reply_text('Invalid location. Please send a Google Maps link, your current location, or a specific address.')
                        return LOCATION_FIND
                except Exception as e:
                    logger.error(f"Geocoding error: {e}")
                    await update.message.reply_text('An error occurred while processing your location. Please try again.')
                    return LOCATION_FIND
    else:
        await update.message.reply_text('Invalid location. Please send a Google Maps link, your current location, or a specific address.')
        return LOCATION_FIND

    blood_type = context.user_data['blood_type']
    donors = await find_nearest_donors(latitude, longitude, blood_type)
    
    if donors:
        donor_info = '\n'.join([f"{name} - {contact} ({distance:.2f} km)" for name, contact, distance in donors])
        await update.message.reply_text(f'Found the following donors near you:\n\n{donor_info}')
    else:
        await update.message.reply_text('No donors found nearby. Please try again later or broaden your search.')
    
    return ConversationHandler.END

async def show_profile(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        row = await conn.fetchrow("""
        SELECT name, blood_type, latitude, longitude, contact, last_donation
        FROM donors
        WHERE user_id = $1
        """, user_id)
        await conn.close()
        
        if row:
            last_donation = row['last_donation'] or 'Never'
            await update.callback_query.message.reply_text(
                f"👤 *Your Profile*\n\n"
                f"Name: {row['name']}\n"
                f"Blood Type: {row['blood_type']}\n"
                f"Contact: {row['contact']}\n"
                f"Last Donation: {last_donation}\n"
                f"Location: {row['latitude']:.6f}, {row['longitude']:.6f}",
                parse_mode='Markdown'
            )
        else:
            await update.callback_query.message.reply_text("Profile not found.")
    except Exception as e:
        logger.error(f"Error fetching profile: {e}")
        await update.callback_query.message.reply_text('An error occurred while fetching your profile. Please try again later.')
    
    return ConversationHandler.END

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            MENU: [CallbackQueryHandler(menu_callback)],
            LOCATION: [MessageHandler(filters.LOCATION | filters.TEXT, location)],
            BLOOD_TYPE: [CallbackQueryHandler(blood_type_callback, pattern=r'^blood_')],
            CONTACT: [MessageHandler(filters.TEXT, contact)],
            PROFILE: [MessageHandler(filters.TEXT, profile)],
            LOCATION_FIND: [MessageHandler(filters.LOCATION | filters.TEXT, location_find)],
            EMERGENCY: [CallbackQueryHandler(emergency_callback, pattern=r'^blood_')],
        },
        fallbacks=[CommandHandler('start', start)],
    )

    app.add_handler(conv_handler)

    logger.info("Starting bot...")
    keep_alive()  # Keep the bot running

    # Run the bot with the correct event loop
    asyncio.run(app.run_polling())

if __name__ == '__main__':
    # Ensure the database is set up in the current event loop
    asyncio.run(setup_database())
    main()