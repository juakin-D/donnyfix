from flask import Flask, render_template, request, redirect, url_for
import sqlite3

app = Flask(__name__)
import os

DATABASE = os.path.join(os.path.dirname(__file__), 'bookings.db')

def init_db():

    #""" this funtion creates the database tables """

    conn =  sqlite3.connect(DATABASE) # OPEN OR CREATE THE DATABASE
    cursor = conn.cursor()            # cursor lrt us run sql commands

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bookings(
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL,
            phone    TEXT NOT NULL,
             email    TEXT NOT NULL,
            device   TEXT NOT NULL,
            service  TEXT NOT NULL,
            date     TEXT NOT NULL,
            notes    TEXT 
        )
   ''' )
    conn.commit()    #  save , writes changes to the file
    conn.close()     # close the connection when done

@app.route('/')
def home():
    return render_template('index.html')


# methods=['GET', 'POST'] This route handles TWO situations:
# GET
# POST

@app.route('/booking',methods=['GET', 'POST'])
def booking():
    print(f" Request method: {request.method}")
    if request.method == 'POST':

        name     =  request.form['name']
        phone    =  request.form['phone']
        email    =  request.form['email']
        device   =  request.form['device']
        service  =  request.form['service']
        date     =  request.form['date']
        notes     =  request.form.get('notes', '') # .get()= safe so it wont crash when empty.

        conn= sqlite3.connect(DATABASE)
        cursor= conn.cursor()
        cursor.execute('''
                       INSERT INTO bookings (name, phone, email, device, service, date, notes)
                       VALUES (?,?,?,?,?,?,?)''', (name, phone, email, device, service, date, notes))
        
        conn.commit()
        conn.close()
            # save the booking to the database.

        print(f"📥 New Booking save for {name}")

        # pass data to a confirmation page.
        return render_template('confirmation.html',
                    name=name,
                    phone=phone,
                    email=email,
                    device=device,
                    service=service,
                    date=date,
                    notes=notes)
    return render_template('booking.html')

# Admin view all bookings
@app.route('/admin')
def admin():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory= sqlite3.Row # acces columns by name
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bookings ORDER BY date DESC")
    bookings = cursor.fetchall()
    conn.close()

    return render_template('admin.html', bookings=bookings)


#DELETE A BOOKING ROUTE
@app.route('/admin/delete/<int:booking_id>', methods=['POST'])
def delete_booking(booking_id):
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()
    cursor.execute('DELETE FROM bookings WHERE id = ?', (booking_id,))
    conn.commit()
    conn.close()

    print("Booking deleted: " + str(booking_id))

    return redirect(url_for('admin'))

init_db()   # initialize the database when the app starts.  
if __name__ == '__main__':    
    app.run(debug=False)