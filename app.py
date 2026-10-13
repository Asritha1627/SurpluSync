
import os
from datetime import datetime, timedelta

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    jsonify,
    flash
)
import mysql.connector
from mysql.connector import Error
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "surplus-sync-local-key")

ALLOWED_STATUSES = ("Available", "Claimed", "Collected")
EXPIRY_WARNING_HOURS = 24


# ----------------------------------------
# DATABASE CONNECTION
# ----------------------------------------

def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "surplus_sync")
    )


# ----------------------------------------
# HELPER: EXPIRY INFORMATION
# ----------------------------------------

def add_expiry_info(donation):
    """
    Adds expiry alert information to a donation.
    This does not change the database status.
    """
    expiry = donation.get("expiry")

    if isinstance(expiry, str):
        try:
            expiry = datetime.fromisoformat(expiry)
        except ValueError:
            donation["expiry_alert"] = "Unknown"
            donation["hours_remaining"] = None
            return donation

    if not expiry:
        donation["expiry_alert"] = "Unknown"
        donation["hours_remaining"] = None
        return donation

    remaining = expiry - datetime.now()
    donation["hours_remaining"] = round(
        remaining.total_seconds() / 3600, 1
    )

    if remaining.total_seconds() <= 0:
        donation["expiry_alert"] = "Expired"
    elif remaining <= timedelta(hours=EXPIRY_WARNING_HOURS):
        donation["expiry_alert"] = "Expiring Soon"
    else:
        donation["expiry_alert"] = "Fresh"

    return donation


# ----------------------------------------
# HOME PAGE
# ----------------------------------------

@app.route("/")
def home():
    return render_template("index.html")


# ----------------------------------------
# ADD FOOD DONATION
# ----------------------------------------

@app.route("/donate", methods=["GET", "POST"])
def donate():
    if request.method == "GET":
        return render_template("donate.html")

    donor = request.form.get("donor", "").strip()
    food_name = request.form.get("food_name", "").strip()
    location = request.form.get("location", "").strip()
    quantity_text = request.form.get("quantity", "").strip()
    expiry_text = request.form.get("expiry", "").strip()

    if not all([donor, food_name, location, quantity_text, expiry_text]):
        flash("Please fill in all fields.", "error")
        return redirect(url_for("donate"))

    try:
        quantity = int(quantity_text)
        if quantity <= 0:
            raise ValueError
    except ValueError:
        flash("Quantity must be a positive whole number.", "error")
        return redirect(url_for("donate"))

    try:
        expiry = datetime.strptime(expiry_text, "%Y-%m-%dT%H:%M")
    except ValueError:
        flash("Please enter a valid expiry date and time.", "error")
        return redirect(url_for("donate"))

    if expiry <= datetime.now():
        flash("Expiry date and time must be in the future.", "error")
        return redirect(url_for("donate"))

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        query = """
            INSERT INTO donations
                (donor, food_name, quantity, location, expiry, status)
            VALUES (%s, %s, %s, %s, %s, %s)
        """

        cursor.execute(
            query,
            (
                donor,
                food_name,
                quantity,
                location,
                expiry,
                "Available"
            )
        )

        connection.commit()
        flash("Food donation added successfully!", "success")
        return redirect(url_for("donation_list"))

    except Error as error:
        app.logger.error("Database error while adding donation: %s", error)
        if connection:
            connection.rollback()
        flash("Could not save the donation. Please check the database.", "error")
        return redirect(url_for("donate"))

    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


# ----------------------------------------
# VIEW ACTIVE DONATIONS + EXPIRY ALERTS
# ----------------------------------------

@app.route("/donations")
def donation_list():
    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT id, donor, food_name, quantity, location,
                   expiry, status, created_at
            FROM donations
            ORDER BY expiry ASC
        """)

        records = cursor.fetchall()

        for donation in records:
            add_expiry_info(donation)

        # Collected donations remain in the database for history,
        # but do not appear in the active donations list.
        active_donations = [
            donation for donation in records
            if donation["status"] != "Collected"
        ]

        available_count = sum(
            1 for donation in active_donations
            if donation["status"] == "Available"
            and donation["expiry_alert"] != "Expired"
        )

        expiring_count = sum(
            1 for donation in active_donations
            if donation["status"] != "Collected"
            and donation["expiry_alert"] == "Expiring Soon"
        )

        expired_count = sum(
            1 for donation in active_donations
            if donation["expiry_alert"] == "Expired"
        )

        total_portions = sum(
            donation["quantity"] for donation in active_donations
            if donation["expiry_alert"] != "Expired"
        )

        return render_template(
            "donations.html",
            donations=active_donations,
            allowed_statuses=ALLOWED_STATUSES,
            available_count=available_count,
            expiring_count=expiring_count,
            expired_count=expired_count,
            total_portions=total_portions
        )

    except Error as error:
        app.logger.error("Database error while listing donations: %s", error)
        return "Could not load donations. Please check the database connection.", 500

    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


# ----------------------------------------
# UPDATE DONATION STATUS
# ----------------------------------------

@app.route(
    "/donations/<int:donation_id>/status",
    methods=["POST"]
)
def update_donation_status(donation_id):
    new_status = request.form.get("status", "").strip()

    if new_status not in ALLOWED_STATUSES:
        flash("Invalid donation status.", "error")
        return redirect(url_for("donation_list"))

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute(
            "SELECT expiry FROM donations WHERE id = %s",
            (donation_id,)
        )
        donation = cursor.fetchone()

        if not donation:
            flash("Donation not found.", "error")
            return redirect(url_for("donation_list"))

        expiry = donation["expiry"]

        # Do not allow expired food to be marked Available or Claimed.
        if expiry <= datetime.now() and new_status != "Collected":
            flash(
                "This food has expired. It cannot be marked Available or Claimed.",
                "error"
            )
            return redirect(url_for("donation_list"))

        cursor.execute(
            "UPDATE donations SET status = %s WHERE id = %s",
            (new_status, donation_id)
        )

        connection.commit()
        flash("Donation status updated successfully!", "success")

    except Error as error:
        app.logger.error("Database error while updating status: %s", error)
        if connection:
            connection.rollback()
        flash("Could not update the donation status.", "error")

    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()

    return redirect(url_for("donation_list"))


# ----------------------------------------
# API: ALL DONATIONS WITH EXPIRY ALERTS
# ----------------------------------------

@app.route("/api/donations")
def api_donations():
    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT id, donor, food_name, quantity, location,
                   expiry, status, created_at
            FROM donations
            ORDER BY expiry ASC
        """)

        records = cursor.fetchall()

        for donation in records:
            add_expiry_info(donation)

            if donation.get("expiry"):
                donation["expiry"] = donation["expiry"].isoformat()

            if donation.get("created_at"):
                donation["created_at"] = donation["created_at"].isoformat()

        return jsonify({
            "count": len(records),
            "donations": records
        })

    except Error as error:
        app.logger.error("Database error in donations API: %s", error)
        return jsonify({"error": "Could not fetch donations"}), 500

    finally:
        if cursor:
            cursor.close()
        if connection and connection.is_connected():
            connection.close()


# ----------------------------------------
# API: DATABASE CONNECTION TEST
# ----------------------------------------

@app.route("/api/db-test")
def db_test():
    connection = None

    try:
        connection = get_db_connection()

        if connection.is_connected():
            return jsonify({
                "status": "success",
                "message": "MySQL Connected Successfully!"
            })

        return jsonify({
            "status": "error",
            "message": "MySQL connection failed"
        }), 500

    except Error as error:
        app.logger.error("Database connection test failed: %s", error)
        return jsonify({
            "status": "error",
            "message": "Could not connect to MySQL"
        }), 500

    finally:
        if connection and connection.is_connected():
            connection.close()


# ----------------------------------------
# API: APPLICATION STATUS
# ----------------------------------------

@app.route("/api/status")
def api_status():
    return jsonify({
        "app": "SurplusSync",
        "status": "running",
        "expiry_alert_hours": EXPIRY_WARNING_HOURS
    })


# ----------------------------------------
# RUN APPLICATION
# ----------------------------------------

if __name__ == "__main__":
    app.run(debug=True)