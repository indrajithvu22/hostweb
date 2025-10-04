# app.py (REVISED)

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from oauth2client.service_account import ServiceAccountCredentials
import gspread
import time
from datetime import datetime
import json
import os

app = Flask(__name__)
# IMPORTANT: Change this to a long, complex secret key for session security!
app.secret_key = 'your_super_secret_key_here' 

# --- CONFIGURATION ---
SERVICE_ACCOUNT_FILE = 'credentials.json' 
SPREADSHEET_NAME = 'Registrations' # The name of your Google Sheet
SHEET_NAME = 'Students' # The name of the tab holding student/RFID data
TIMEOUT_SECONDS = 300 # 5 minutes for the linking process

# Dictionary to hold students who have registered and are waiting for scan.
# Key: Register Number, Value: {'name': name, 'timestamp': time.time(), 'row_index': N}
WAITING_FOR_SCAN = {}

# --- GOOGLE SHEETS SETUP ---

def get_sheet():
    """Initializes and returns the Google Sheet client and the main worksheet."""
    try:
        # Service account scope and authorization
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
        client = gspread.authorize(creds)
        
        # Open spreadsheet and worksheet
        spreadsheet = client.open(SPREADSHEET_NAME)
        worksheet = spreadsheet.worksheet(SHEET_NAME)
        return worksheet
    except Exception as e:
        print(f"Error accessing Google Sheet: {e}")
        return None

# --- HELPER FUNCTION: LINKS UID TO STUDENT DATA IN GOOGLE SHEETS ---

def link_uid_to_student(row_index, rfid_uid, name):
    """Updates the RFID UID cell using the pre-calculated row_index."""
    sheet = get_sheet()
    if not sheet:
        return False, "Database connection failed."

    try:
        # Assuming RFID UID is in Column 3 (C)
        # CHANGE THIS NUMBER if 'RFID UID' is not the 3rd column in your sheet!
        RFID_UID_COLUMN = 3 
        
        # Update the specific cell using the saved row_index
        sheet.update_cell(row_index, RFID_UID_COLUMN, rfid_uid)

        # Assuming Name is in Column 2 (B) for verification
        return True, sheet.cell(row_index, 2).value 

    except Exception as e:
        print(f"Error updating sheet at row {row_index}: {e}")
        return False, f"Failed to update database. Error: {e}"

# --- WEB APP ROUTES ---

@app.route('/', methods=['GET'])
def index():
    """Renders the main registration form."""
    return render_template('register.html')

@app.route('/register', methods=['POST'])
def register():
    """Handles form submission, appends row to sheet, and initiates the waiting state."""
    name = request.form.get('name').strip()
    reg_no = request.form.get('reg_no').strip()

    if not name or not reg_no:
        return jsonify({'status': 'error', 'message': 'Name and Register Number are required.'})
    
    sheet = get_sheet()
    if not sheet:
        return jsonify({'status': 'error', 'message': 'Could not connect to database.'}), 500

    try:
        # Data to append: [RegNo, Name, RFID_UID (blank), Date Added]
        data_row = [reg_no, name, '', datetime.now().strftime('%Y-%m-%d %H:%M:%S')]
        
        # Append the new row to the sheet
        result = sheet.append_row(data_row, value_input_option='USER_ENTERED')

        # --- CRITICAL STEP: Get the new row index ---
        # The result object gives the range (e.g., 'Students!A5'). We extract the row number (5).
        added_range = result['updates']['updatedRange']
        new_row_index = int(''.join(filter(str.isdigit, added_range)))
        
        # Add the student to the waiting list with the row index
        WAITING_FOR_SCAN[reg_no] = {
            'name': name, 
            'timestamp': time.time(),
            'row_index': new_row_index 
        }
        session['waiting_reg_no'] = reg_no
        
        print(f"Student {name} ({reg_no}) added to row {new_row_index}. Waiting for scan...")
        
        return jsonify({
            'status': 'waiting', 
            'message': f"Student {name} registered. Waiting for RFID scan...",
            'reg_no': reg_no
        })

    except Exception as e:
        print(f"Error appending row to Google Sheet: {e}")
        return jsonify({'status': 'error', 'message': f'Failed to save registration data: {e}'}), 500


@app.route('/check_status/<reg_no>', methods=['GET'])
def check_status(reg_no):
    """Called by the browser to check if the student has been linked or timed out."""
    if reg_no not in WAITING_FOR_SCAN:
        if session.get('linked_reg_no') == reg_no:
            session.pop('linked_reg_no', None)
            return jsonify({'status': 'linked', 'message': 'Card linked successfully!'})
        
        return jsonify({'status': 'error', 'message': 'Session expired or invalid.'})
    
    # Check for timeout
    if time.time() - WAITING_FOR_SCAN[reg_no]['timestamp'] > TIMEOUT_SECONDS:
        name = WAITING_FOR_SCAN.pop(reg_no)['name']
        session.pop('waiting_reg_no', None)
        print(f"Linking session timed out for: {name} ({reg_no})")
        return jsonify({'status': 'timeout', 'message': 'Linking session timed out.'}), 408
        
    return jsonify({'status': 'waiting', 'message': 'Still waiting for RFID scan...'})


# --- ESP32 ROUTE ---

@app.route('/link_rfid', methods=['POST'])
def link_rfid():
    """Endpoint called by the ESP32 to update the new row with the RFID UID."""
    data = request.get_json()
    rfid_uid = data.get('rfid_uid', '').strip()
    
    print(f"Received UID from ESP32: {rfid_uid}")
    
    if not WAITING_FOR_SCAN:
        print("LINKING FAILED: No student is currently waiting for RFID linking.")
        return jsonify({'status': 'error', 'message': 'No active linking session found.'}), 404

    try:
        # Find the oldest waiting student (who initiated the request)
        waiting_reg_no = min(WAITING_FOR_SCAN, key=lambda k: WAITING_FOR_SCAN[k]['timestamp'])
        student_data = WAITING_FOR_SCAN[waiting_reg_no]
        
        row_to_update = student_data['row_index']
        student_name = student_data['name']
        
        print(f"Attempting to link UID {rfid_uid} to RegNo {waiting_reg_no} at row {row_to_update}")

        # 1. Update Google Sheet using the saved row index
        success, result_name = link_uid_to_student(row_to_update, rfid_uid, student_name)
        
        if success:
            # 2. Clean up local state
            session['linked_reg_no'] = waiting_reg_no
            WAITING_FOR_SCAN.pop(waiting_reg_no) 
            
            # 3. Respond to ESP32
            print(f"LINKING SUCCESS: {result_name} linked with {rfid_uid}")
            return jsonify({
                'status': 'linked', 
                'name': result_name, 
                'register_number': waiting_reg_no,
                'message': f"Card linked to {result_name}"
            })
        else:
            print(f"LINKING FAILED (Sheet Error): {result_name}")
            return jsonify({'status': 'error', 'message': result_name}), 500

    except Exception as e:
        print(f"Critical error during /link_rfid: {e}")
        return jsonify({'status': 'error', 'message': 'Internal server error during linking.'}), 500


# --- RUN THE APP ---

if __name__ == '__main__':
    # CRITICAL: host='0.0.0.0' allows external devices (ESP32) to connect.
    app.run(debug=True, host='0.0.0.0')