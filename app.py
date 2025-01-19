from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, render_template_string, make_response, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date, timedelta
import os
import io
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
import pandas as pd  # Import pandas
import openpyxl
import xlrd
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from pyotp import random_base32, TOTP
from flask_caching import Cache
from whitenoise import WhiteNoise

app = Flask(__name__)

# Configuration
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///inventory.db')
if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgres://'):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_size': 10,
    'max_overflow': 20,
    'pool_pre_ping': True,
    'pool_recycle': 300,
}

# Cache configuration
app.config['CACHE_TYPE'] = 'SimpleCache'
app.config['CACHE_DEFAULT_TIMEOUT'] = 300
cache = Cache(app)

# Static files serving with WhiteNoise
app.wsgi_app = WhiteNoise(
    app.wsgi_app,
    root='static/',
    prefix='static/',
    max_age=31536000
)

db = SQLAlchemy(app)

# Models
class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_code = db.Column(db.String(20), unique=True, nullable=False)
    description = db.Column(db.String(200), nullable=False)
    tamil_name = db.Column(db.String(200))  # Optional Tamil name
    uom = db.Column(db.String(10), nullable=False)
    price = db.Column(db.Float, nullable=False)
    stock = db.Column(db.Integer, default=0)
    restock_level = db.Column(db.Integer, default=0)  # Level at which to restock

    @property
    def serialize(self):
        return {
            'id': self.id,
            'item_code': self.item_code,
            'description': self.description,
            'tamil_name': self.tamil_name,
            'uom': self.uom,
            'price': self.price,
            'stock': self.stock,
            'restock_level': self.restock_level
        }

class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_number = db.Column(db.String(3), nullable=False)  # Daily 3-digit number
    date = db.Column(db.Date, nullable=False, default=date.today)
    customer_name = db.Column(db.String(200))
    total_amount = db.Column(db.Float, default=0.0)
    total_items = db.Column(db.Integer, default=0)
    items = db.relationship('InvoiceItem', backref='invoice', lazy=True, cascade="all, delete-orphan")

    @classmethod
    def generate_order_number(cls):
        # Get the latest invoice for today
        today = date.today()
        latest_invoice = cls.query.filter(
            db.func.date(cls.date) == today
        ).order_by(cls.order_number.desc()).first()
        
        if latest_invoice:
            last_number = int(latest_invoice.order_number)
            new_number = str(last_number + 1).zfill(3)
        else:
            new_number = '001'
        
        return new_number

class InvoiceItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey('invoice.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Float, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    product = db.relationship('Product', backref='invoice_items')

class PrintTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(20), nullable=False)  # 'invoice' or 'summary'
    content = db.Column(db.Text, nullable=False)
    is_default = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# User model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')
    totp_secret = db.Column(db.String(32))
    totp_enabled = db.Column(db.Boolean, default=False)

    def verify_totp(self, token):
        if not self.totp_enabled or not self.totp_secret:
            return True
        totp = TOTP(self.totp_secret)
        return totp.verify(token)

class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    calculator_code = db.Column(db.String(20), default='9999')

# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Admin required decorator
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        user = User.query.get(session['user_id'])
        if not user or user.role != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

# Add helper function for getting user info
def get_user(user_id):
    return User.query.get(user_id)

# Add to template context
app.jinja_env.globals.update(get_user=get_user)

# Create tables and add sample data
def init_db():
    with app.app_context():
        # Drop all tables
        db.drop_all()
        # Create all tables
        db.create_all()
        
        # Create admin user if none exists
        if not User.query.filter_by(role='admin').first():
            admin_user = User(
                username='admin',
                password=generate_password_hash('admin'),
                role='admin'
            )
            db.session.add(admin_user)
            db.session.commit()
        
        # Add some sample products with restock levels
        sample_products = [
            Product(item_code='2714', description='AGAL FANCY RPS 1', uom='PCS', price=55.0, stock=100, restock_level=50),
            Product(item_code='33846', description='11423 3 CANDANPIYALI', uom='PCS', price=210.0, stock=50, restock_level=75),
            Product(item_code='2959', description='ABISHEKAM SIVLING RPS', uom='kgs', price=900.0, stock=75, restock_level=100),
            Product(item_code='2324', description='AGAL KAMAL DEEP 1 LW', uom='PCS', price=60.0, stock=200, restock_level=150),
        ]
        
        for product in sample_products:
            db.session.add(product)
        
        db.session.commit()

# Helper function to delete invoices and invoice items
def _delete_all_invoices():
    InvoiceItem.query.delete()
    Invoice.query.delete()

# Helper function to delete products, invoices and invoice items
def _delete_all_products():
    InvoiceItem.query.delete()
    Invoice.query.delete()
    Product.query.delete()

# Routes
@app.route('/')
@login_required
def index():
    # Get products that are at or below their restock level
    low_stock_products = Product.query.filter(Product.stock <= Product.restock_level).all()
    
    # Calculate total inventory cost
    total_inventory_cost = sum(product.stock * product.price for product in Product.query.all())
    
    # Calculate stock-out count
    stockout_count = Product.query.filter(Product.stock == 0).count()
    
    # Calculate average stock-to-sales ratio
    products = Product.query.all()
    total_ratio = 0
    products_with_sales = 0
    for product in products:
        total_sales = db.session.query(func.sum(InvoiceItem.quantity)).filter(
            InvoiceItem.product_id == product.id
        ).scalar() or 0
        if total_sales > 0:
            total_ratio += product.stock / total_sales
            products_with_sales += 1
    avg_stock_sales_ratio = total_ratio / products_with_sales if products_with_sales > 0 else 0
    
    # Get slow-moving products count (no sales in last 30 days)
    thirty_days_ago = datetime.now() - timedelta(days=30)
    slow_moving_count = 0
    for product in products:
        recent_sales = db.session.query(InvoiceItem).join(Invoice).filter(
            InvoiceItem.product_id == product.id,
            Invoice.date >= thirty_days_ago
        ).count()
        if recent_sales == 0:
            slow_moving_count += 1
    
    # Calculate stock-out frequency
    stockout_frequency = []
    for product in products:
        if product.stock == 0:
            # You would need to track stock-out history in a separate table
            # This is a simplified version
            stockout_frequency.append({
                'product_name': product.description,
                'stockout_count': 1,
                'last_stockout': datetime.now().strftime('%Y-%m-%d'),
                'avg_duration': 0,
                'risk_level': 'High',
                'risk_level_color': 'danger'
            })
    
    stats = {
        'total_products': Product.query.count(),
        'total_invoices': Invoice.query.count(),
        'total_sales': db.session.query(db.func.sum(Invoice.total_amount)).scalar() or 0,
        'recent_invoices': Invoice.query.order_by(Invoice.date.desc()).limit(5).all(),
        'low_stock_products': low_stock_products,
        'total_inventory_cost': total_inventory_cost,
        'stockout_count': stockout_count,
        'avg_stock_sales_ratio': avg_stock_sales_ratio,
        'slow_moving_count': slow_moving_count,
        'stockout_frequency': stockout_frequency
    }
    return render_template('index.html', stats=stats)

@app.route('/products/search')
@login_required
def search_products():
    query = request.args.get('q', '').lower()
    # Split the query into words and remove empty strings
    search_terms = [term.strip() for term in query.split() if term.strip()]
    
    if not search_terms:
        return jsonify([])
    
    # Get all products
    products = Product.query.all()
    
    # Filter products based on search terms
    filtered_products = []
    for product in products:
        description_lower = product.description.lower()
        item_code_lower = product.item_code.lower()
        
        # Check if all search terms are present in either item code or description
        matches = all(
            term in description_lower.replace(' ', '') or 
            term in item_code_lower.replace(' ', '') or
            term in description_lower or
            term in item_code_lower
            for term in search_terms
        )
        
        if matches:
            filtered_products.append(product)
    
    return jsonify([product.serialize for product in filtered_products])

@app.route('/products', methods=['GET', 'POST'])
@login_required
def products():
    if request.method == 'POST':
        data = request.json
        
        # Server-side validation
        if not all(key in data for key in ('item_code', 'description', 'uom', 'price', 'stock')):
            return jsonify({'success': False, 'error': 'Missing required data'})
        
        try:
            price = float(data['price'])
            stock = int(data['stock'])
            restock_level = int(data.get('restock_level', 0))
        except ValueError as e:
            return jsonify({'success': False, 'error': f'Invalid price, stock, or restock level format: {str(e)}'})
            
        product = Product(
            item_code=data['item_code'],
            description=data['description'],
            tamil_name=data.get('tamil_name', ''),
            uom=data['uom'],
            price=price,
            stock=stock,
            restock_level=restock_level
        )
        try:
            db.session.add(product)
            db.session.commit()
            return jsonify({'success': True})
        except IntegrityError as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': 'Database integrity error (e.g., duplicate item code).'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})
    
    products = Product.query.all()
    return render_template('products.html', products=products)

@app.route('/products/<int:id>', methods=['PUT', 'DELETE'])
@login_required
def product(id):
    product = Product.query.get_or_404(id)
    
    if request.method == 'PUT':
        data = request.json
        
        # Server-side validation
        if not all(key in data for key in ('item_code', 'description', 'uom', 'price', 'stock')):
            return jsonify({'success': False, 'error': 'Missing required data'})
        
        try:
            price = float(data['price'])
            stock = int(data['stock'])
            restock_level = int(data.get('restock_level', 0))
        except ValueError as e:
            return jsonify({'success': False, 'error': f'Invalid price, stock, or restock level format: {str(e)}'})
        
        product.item_code = data['item_code']
        product.description = data['description']
        product.tamil_name = data.get('tamil_name', '')
        product.uom = data['uom']
        product.price = price
        product.stock = stock
        product.restock_level = restock_level
        
        try:
            db.session.commit()
            return jsonify({'success': True})
        except IntegrityError as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': 'Database integrity error (e.g., duplicate item code).'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})
    
    elif request.method == 'DELETE':
        try:
            db.session.delete(product)
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})

@app.route('/invoices')
def invoices():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    query = Invoice.query
    if start_date and end_date:
        query = query.filter(
            Invoice.date >= datetime.strptime(start_date, '%Y-%m-%d').date(),
            Invoice.date <= datetime.strptime(end_date, '%Y-%m-%d').date()
        )
    
    invoices = query.order_by(Invoice.date.desc()).all()
    return render_template('invoices.html', invoices=invoices)

@app.route('/invoices/<int:id>', methods=['GET', 'PUT', 'DELETE'])
def invoice(id):
    invoice = Invoice.query.get_or_404(id)
    
    if request.method == 'GET':
        return jsonify({
            'id': invoice.id,
            'order_number': invoice.order_number,
            'date': invoice.date.isoformat(),
            'customer_name': invoice.customer_name,
            'total_amount': invoice.total_amount,
            'total_items': invoice.total_items,
            'items': [{
                'id': item.id,
                'product_id': item.product_id,
                'product_code': item.product.item_code,
                'description': item.product.description,
                'uom': item.product.uom,
                'quantity': item.quantity,
                'price': item.price,
                'amount': item.amount
            } for item in invoice.items]
        })
    
    elif request.method == 'PUT':
        data = request.json
        invoice.customer_name = data['customer_name']
        invoice.total_amount = float(data['total_amount'])
        invoice.total_items = int(data['total_items'])
        
        # Restore stock for existing items
        for item in invoice.items:
            item.product.stock += item.quantity
        
        # Remove existing items
        for item in invoice.items:
            db.session.delete(item)
        
        # Add new items
        for item_data in data['items']:
            product = Product.query.get(item_data['product_id'])
            # Remove stock validation check
            item = InvoiceItem(
                invoice=invoice,
                product_id=item_data['product_id'],
                quantity=item_data['quantity'],
                price=item_data['price'],
                amount=item_data['amount']
            )
            product.stock -= item_data['quantity']
            db.session.add(item)
        
        try:
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})
    
    elif request.method == 'DELETE':
        data = request.json
        restore_stock = data.get('restore_stock', False)
        
        try:
            if restore_stock:
                # Restore stock for all items
                for item in invoice.items:
                    item.product.stock += item.quantity
            
            db.session.delete(invoice)
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})

@app.route('/invoices/<int:id>/print')
def print_invoice(id):
    invoice = Invoice.query.get_or_404(id)
    template = get_default_template('invoice')
    return render_template_string(template.content, invoice=invoice)

@app.route('/new_invoice', methods=['GET', 'POST'])
def new_invoice():
    if request.method == 'POST':
        data = request.json
        invoice = Invoice(
            order_number=Invoice.generate_order_number(),
            date=datetime.strptime(data['date'], '%Y-%m-%d').date(),
            customer_name=data['customer_name'],
            total_amount=float(data['total_amount']),
            total_items=int(data['total_items'])
        )
        
        try:
            db.session.add(invoice)
            
            for item_data in data['items']:
                product = Product.query.get(item_data['product_id'])
                # Remove stock validation check
                invoice_item = InvoiceItem(
                    invoice=invoice,
                    product_id=item_data['product_id'],
                    quantity=item_data['quantity'],
                    price=item_data['price'],
                    amount=item_data['amount']
                )
                
                # Update product stock (allow negative values)
                product.stock -= item_data['quantity']
                db.session.add(invoice_item)
            
            db.session.commit()
            return jsonify({'success': True, 'id': invoice.id})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})
    
    products = Product.query.all()
    return render_template('new_invoice.html', products=products)

@app.route('/invoices/print_summary')
def print_summary():
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')
    
    query = Invoice.query
    if start_date and end_date:
        start = datetime.strptime(start_date, '%Y-%m-%d').date()
        end = datetime.strptime(end_date, '%Y-%m-%d').date()
        query = query.filter(Invoice.date >= start, Invoice.date <= end)
    
    invoices = query.order_by(Invoice.date.desc()).all()
    total_items = sum(invoice.total_items for invoice in invoices)
    total_amount = sum(invoice.total_amount for invoice in invoices)
    
    template = get_default_template('summary')
    return render_template_string(template.content,
        invoices=invoices,
        start_date=start_date and datetime.strptime(start_date, '%Y-%m-%d').date(),
        end_date=end_date and datetime.strptime(end_date, '%Y-%m-%d').date(),
        total_items=total_items,
        total_amount=total_amount
    )

@app.route('/products/import', methods=['POST'])
def import_products():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'})

    file = request.files['file']
    filename = file.filename

    if not filename.endswith(('.xlsx', '.xls', '.csv')):
        return jsonify({'success': False, 'error': 'Invalid file format. Please upload .xlsx, .xls, or .csv file'})

    try:
        # Save the uploaded file temporarily
        temp_path = os.path.join(os.getcwd(), 'temp_upload' + os.path.splitext(filename)[1])
        file.save(temp_path)
        
        try:
            if filename.endswith('.xlsx'):
                # For XLSX files
                wb = openpyxl.load_workbook(temp_path, read_only=True, data_only=True)
                ws = wb.active
                rows = list(ws.rows)
                
                # Skip header row and process data
                products = []
                for row in rows[1:]:  # Skip header row
                    if all(cell.value is None for cell in row):  # Skip empty rows
                        continue
                    try:
                        product = {
                            'item_code': str(row[0].value).strip(),
                            'description': str(row[1].value).strip(),
                            'uom': str(row[2].value).strip(),
                            'price': float(str(row[3].value).replace(',', '')),
                            'stock': int(float(str(row[4].value).replace(',', '')))
                        }
                        if all(product.values()):  # Check if all values are non-empty
                            products.append(product)
                    except (ValueError, AttributeError, IndexError) as e:
                        continue  # Skip rows with invalid data
                wb.close()
                
            elif filename.endswith('.xls'):
                # For XLS files
                wb = xlrd.open_workbook(temp_path)
                ws = wb.sheet_by_index(0)
                
                # Skip header row and process data
                products = []
                for row_idx in range(1, ws.nrows):  # Skip header row
                    try:
                        product = {
                            'item_code': str(ws.cell_value(row_idx, 0)).strip(),
                            'description': str(ws.cell_value(row_idx, 1)).strip(),
                            'uom': str(ws.cell_value(row_idx, 2)).strip(),
                            'price': float(str(ws.cell_value(row_idx, 3)).replace(',', '')),
                            'stock': int(float(str(ws.cell_value(row_idx, 4)).replace(',', '')))
                        }
                        if all(product.values()):  # Check if all values are non-empty
                            products.append(product)
                    except (ValueError, AttributeError, IndexError) as e:
                        continue  # Skip rows with invalid data
                
            else:  # CSV files
                # Try different encodings for CSV
                encodings = ['utf-8', 'latin1', 'iso-8859-1', 'cp1252']
                df = None
                
                for encoding in encodings:
                    try:
                        df = pd.read_csv(temp_path, encoding=encoding)
                        break
                    except:
                        continue
                
                if df is None:
                    raise ValueError("Unable to read CSV file with any supported encoding")
                
                # Process data
                products = []
                for _, row in df.iterrows():
                    try:
                        product = {
                            'item_code': str(row.iloc[0]).strip(),
                            'description': str(row.iloc[1]).strip(),
                            'uom': str(row.iloc[2]).strip(),
                            'price': float(str(row.iloc[3]).replace(',', '')),
                            'stock': int(float(str(row.iloc[4]).replace(',', '')))
                        }
                        if all(product.values()):  # Check if all values are non-empty
                            products.append(product)
                    except (ValueError, AttributeError, IndexError) as e:
                        continue  # Skip rows with invalid data
            
            # Remove temporary file
            os.remove(temp_path)
            
            if not products:
                return jsonify({'success': False, 'error': 'No valid data found in the file'})
            
            # Process the products
            for product_data in products:
                product = Product.query.filter_by(item_code=product_data['item_code']).first()
                if product:
                    # Update existing product
                    product.description = product_data['description']
                    product.uom = product_data['uom']
                    product.price = product_data['price']
                    product.stock = product_data['stock']
                else:
                    # Create new product
                    product = Product(**product_data)
                    db.session.add(product)
            
            db.session.commit()
            return jsonify({'success': True, 'message': f'Successfully imported {len(products)} products'})
            
        except Exception as e:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return jsonify({'success': False, 'error': f'Error processing file: {str(e)}'})
            
    except Exception as e:
        return jsonify({'success': False, 'error': f'Error uploading file: {str(e)}'})

@app.route('/products/export')
def export_products():
    products = Product.query.all()
    
    # Use pandas to export
    df = pd.DataFrame([p.__dict__ for p in products])
    df.rename(columns={'item_code': 'Item Code', 'description': 'Description',
                       'uom': 'UOM', 'price': 'Price', 'stock': 'Stock'}, inplace=True)

    output = io.BytesIO()
    writer = pd.ExcelWriter(output, engine='xlsxwriter')  # Use xlsxwriter engine

    df[['Item Code', 'Description', 'UOM', 'Price', 'Stock']].to_excel(writer, sheet_name='Products', index=False)
    
    writer.close() # close the writer

    output.seek(0)

    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='products.xlsx'
    )


@app.route('/settings/print-templates')
def print_templates():
    invoice_templates = PrintTemplate.query.filter_by(type='invoice').all()
    summary_templates = PrintTemplate.query.filter_by(type='summary').all()
    return render_template('settings/print_templates.html',
                         invoice_templates=invoice_templates,
                         summary_templates=summary_templates)

@app.route('/settings/print-templates/new', methods=['GET', 'POST'])
@login_required
@admin_required
def new_print_template():
    if request.method == 'POST':
        data = request.json
        template = PrintTemplate(
            name=data['name'],
            type=data['type'],
            content=data['content'],
            is_default=data.get('is_default', False)
        )
        
        if template.is_default:
            # Remove default flag from other templates of same type
            PrintTemplate.query.filter_by(type=template.type, is_default=True).update({'is_default': False})
        
        try:
            db.session.add(template)
            db.session.commit()
            return jsonify({'success': True, 'id': template.id})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})
    
    return render_template('print_template_form.html', template=None)

@app.route('/settings/print-templates/<int:id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
@admin_required
def print_template(id):
    template = PrintTemplate.query.get_or_404(id)
    
    if request.method == 'GET':
        return render_template('print_template_form.html', template=template)
    
    elif request.method == 'PUT':
        data = request.json
        template.name = data['name']
        template.content = data['content']
        template.is_default = data.get('is_default', False)
        
        if template.is_default:
            # Remove default flag from other templates of same type
            PrintTemplate.query.filter(
                PrintTemplate.type == template.type,
                PrintTemplate.id != template.id,
                PrintTemplate.is_default == True
            ).update({'is_default': False})
        
        try:
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})
    
    elif request.method == 'DELETE':
        if template.is_default:
            return jsonify({'success': False, 'error': 'Cannot delete default template'})
        
        try:
            db.session.delete(template)
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'error': str(e)})

def get_default_template(type):
    template = PrintTemplate.query.filter_by(type=type, is_default=True).first()
    if not template:
        # Create default template if none exists
        if type == 'invoice':
            with open('templates/print_invoice.html', 'r') as f:
                content = f.read()
        else:
            with open('templates/print_summary.html', 'r') as f:
                content = f.read()
        
        template = PrintTemplate(
            name=f'Default {type.title()} Template',
            type=type,
            content=content,
            is_default=True
        )
        db.session.add(template)
        db.session.commit()
    
    return template

@app.route('/settings')
@login_required
@admin_required
def settings():
    # Get print templates
    invoice_templates = PrintTemplate.query.filter_by(type='invoice').all()
    summary_templates = PrintTemplate.query.filter_by(type='summary').all()
    
    return render_template('settings.html', 
                         invoice_templates=invoice_templates,
                         summary_templates=summary_templates)

@app.route('/settings/calculator-code', methods=['GET', 'POST'])
@login_required
@admin_required
def calculator_code():
    if request.method == 'POST':
        try:
            data = request.get_json()
            code = data.get('code')
            if not code:
                return jsonify({'error': 'Code is required'}), 400
                
            settings = Settings.query.first()
            if not settings:
                settings = Settings()
                db.session.add(settings)
            
            settings.calculator_code = code
            print(f"Updating calculator code to: {code}")  # Debug log
            
            try:
                db.session.commit()
                print("Database commit successful")  # Debug log
                return jsonify({'success': True, 'code': settings.calculator_code})
            except Exception as e:
                print(f"Database commit failed: {str(e)}")  # Debug log
                db.session.rollback()
                return jsonify({'error': f'Failed to save code: {str(e)}'}), 500
                
        except Exception as e:
            print(f"Error in calculator code update: {str(e)}")  # Debug log
            return jsonify({'error': str(e)}), 500
    else:
        try:
            settings = Settings.query.first()
            code = settings.calculator_code if settings else '9999'
            print(f"Current calculator code: {code}")  # Debug log
            return jsonify({'code': code})
        except Exception as e:
            print(f"Error retrieving calculator code: {str(e)}")  # Debug log
            return jsonify({'error': str(e)}), 500

@app.route('/settings/backup')
@login_required
@admin_required
def backup_data():
    # Get all data from database
    products = Product.query.all()
    invoices = Invoice.query.all()
    invoice_items = InvoiceItem.query.all()
    templates = PrintTemplate.query.all()
    settings = Settings.query.first()
    
    # Create backup data structure
    backup = {
        'products': [{
            'item_code': p.item_code,
            'description': p.description,
            'uom': p.uom,
            'price': p.price,
            'stock': p.stock,
            'restock_level': p.restock_level,
            'tamil_name': p.tamil_name
        } for p in products],
        'invoices': [{
            'order_number': i.order_number,
            'date': i.date.isoformat(),
            'customer_name': i.customer_name,
            'total_amount': i.total_amount,
            'total_items': i.total_items
        } for i in invoices],
        'invoice_items': [{
            'invoice_order_number': ii.invoice.order_number,
            'product_item_code': ii.product.item_code,
            'quantity': ii.quantity,
            'price': ii.price,
            'amount': ii.amount
        } for ii in invoice_items],
        'templates': [{
            'name': t.name,
            'type': t.type,
            'content': t.content,
            'is_default': t.is_default
        } for t in templates],
        'settings': {
            'calculator_code': settings.calculator_code if settings else '9999'
        }
    }
    
    return jsonify(backup)

@app.route('/settings/restore', methods=['POST'])
@login_required
@admin_required
def restore_data():
    try:
        data = request.get_json()
        
        # Clear existing data
        PrintTemplate.query.delete()
        InvoiceItem.query.delete()
        Invoice.query.delete()
        Product.query.delete()
        Settings.query.delete()
        
        # Restore products
        for product_data in data.get('products', []):
            product = Product(**product_data)
            db.session.add(product)
        
        # Restore invoices
        for invoice_data in data.get('invoices', []):
            date_str = invoice_data.pop('date')
            invoice_data['date'] = datetime.fromisoformat(date_str).date()
            invoice = Invoice(**invoice_data)
            db.session.add(invoice)
            
            # Restore invoice items
        for item_data in data.get('invoice_items', []):
            invoice = Invoice.query.filter_by(order_number=item_data['invoice_order_number']).first()
            product = Product.query.filter_by(item_code=item_data['product_item_code']).first()
            if invoice and product:
                item = InvoiceItem(
                    invoice=invoice,
                    product=product,
                    quantity=item_data['quantity'],
                    price=item_data['price'],
                    amount=item_data['amount']
                )
                db.session.add(item)
        
        # Restore templates
        for template_data in data.get('templates', []):
            template = PrintTemplate(**template_data)
            db.session.add(template)
        
        # Restore settings
        settings_data = data.get('settings', {})
        settings = Settings(calculator_code=settings_data.get('calculator_code', '9999'))
        db.session.add(settings)
        
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/settings/delete-invoices', methods=['POST'])
@login_required
@admin_required
def delete_invoices():
    try:
        _delete_all_invoices()
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/settings/delete-products', methods=['POST'])
@login_required
@admin_required
def delete_products():
    try:
        _delete_all_products()
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/settings/delete-all', methods=['POST'])
@login_required
@admin_required
def delete_all():
    try:
        _delete_all_products()  # This also deletes invoices
        PrintTemplate.query.delete()
        settings = Settings.query.first()
        if settings:
            settings.calculator_code = '9999'
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/settings/print-templates/<int:id>', methods=['DELETE'])
@login_required
@admin_required
def delete_print_template(id):
    template = PrintTemplate.query.get_or_404(id)
    if template.is_default:
        return jsonify({'success': False, 'error': 'Cannot delete default template'})
    
    try:
        db.session.delete(template)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/sales_trend')
@cache.cached(timeout=300)
def sales_trend():
    try:
        # Get sales data for the last 30 days
        end_date = date.today()
        start_date = end_date - timedelta(days=29)
        
        sales_data = db.session.query(
            func.date(Invoice.date).label('date'),
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            Invoice.date >= start_date,
            Invoice.date <= end_date
        ).group_by(
            func.date(Invoice.date)
        ).order_by(
            func.date(Invoice.date)
        ).all()
        
        # Create a dictionary of dates and sales
        sales_dict = {row.date.strftime('%Y-%m-%d'): float(row.total) for row in sales_data}
        
        # Fill in missing dates with zero
        labels = []
        values = []
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime('%Y-%m-%d')
            labels.append(date_str)
            values.append(sales_dict.get(date_str, 0))
            current_date += timedelta(days=1)
        
        return jsonify({
            'labels': labels,
            'values': values
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/top_products')
@cache.cached(timeout=300)
def top_products():
    try:
        # Get top 5 selling products
        top_products = db.session.query(
            Product.description,
            func.sum(InvoiceItem.quantity).label('total_quantity')
        ).join(
            InvoiceItem, Product.id == InvoiceItem.product_id
        ).group_by(
            Product.id
        ).order_by(
            func.sum(InvoiceItem.quantity).desc()
        ).limit(5).all()
        
        return jsonify({
            'labels': [p.description for p in top_products],
            'values': [float(p.total_quantity) for p in top_products]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/invoices/delete_all', methods=['DELETE'])
def delete_all_invoices():
    data = request.json
    restore_stock = data.get('restore_stock', False)
    
    try:
        if restore_stock:
            # Restore stock for all items before deleting
            invoices = Invoice.query.all()
            for invoice in invoices:
                for item in invoice.items:
                    item.product.stock += item.quantity
        
        # Delete all invoice items and invoices
        _delete_all_invoices()
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/products/import-json', methods=['POST'])
def import_products_json():
    try:
        data = request.json
        if not data or 'products' not in data:
            return jsonify({'success': False, 'error': 'No data provided'})

        products = data['products']
        if not products:
            return jsonify({'success': False, 'error': 'No products found in data'})

        # Process the products
        for product_data in products:
            # Validate required fields
            if not all(key in product_data for key in ['item_code', 'description', 'uom', 'price', 'stock']):
                continue

            try:
                # Convert types and validate
                product_data['price'] = float(product_data['price'])
                product_data['stock'] = int(product_data['stock'])
                
                # Find existing product or create new one
                product = Product.query.filter_by(item_code=product_data['item_code']).first()
                if product:
                    # Update existing product
                    product.description = product_data['description']
                    product.uom = product_data['uom']
                    product.price = product_data['price']
                    product.stock = product_data['stock']
                else:
                    # Create new product
                    product = Product(**product_data)
                    db.session.add(product)
                    
            except (ValueError, TypeError) as e:
                continue  # Skip invalid rows
                
        db.session.commit()
        return jsonify({
            'success': True,
            'message': f'Successfully imported {len(products)} products'
        })
        
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/invoices/<int:invoice_id>/download')
def download_invoice(invoice_id):
    try:
        # Get invoice data
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get invoice details
        cursor.execute('''
            SELECT i.order_number, i.date, i.customer_name, i.total_amount,
                   ii.product_id, ii.quantity, ii.price, ii.amount,
                   p.name as product_name, p.code as product_code
            FROM invoices i
            JOIN invoice_items ii ON i.id = ii.invoice_id
            JOIN products p ON ii.product_id = p.id
            WHERE i.id = ?
        ''', (invoice_id,))
        
        rows = cursor.fetchall()
        if not rows:
            return 'Invoice not found', 404
            
        # Create invoice data structure
        invoice = {
            'order_number': rows[0][0],
            'date': rows[0][1],
            'customer_name': rows[0][2],
            'total_amount': rows[0][3],
            'items': []
        }
        
        for row in rows:
            invoice['items'].append({
                'product_code': row[9],
                'product_name': row[8],
                'quantity': row[5],
                'price': row[6],
                'amount': row[7]
            })
        
        # Create PDF
        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=letter)
        elements = []
        styles = getSampleStyleSheet()
        
        # Add title
        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=16,
            spaceAfter=30
        )
        elements.append(Paragraph(f"Invoice #{invoice['order_number']}", title_style))
        
        # Add invoice details
        elements.append(Paragraph(f"Date: {invoice['date']}", styles['Normal']))
        elements.append(Paragraph(f"Customer: {invoice['customer_name']}", styles['Normal']))
        elements.append(Spacer(1, 20))
        
        # Create table for items
        data = [['S.No', 'Code', 'Product', 'Quantity', 'Price', 'Total']]
        for idx, item in enumerate(invoice['items'], 1):
            data.append([
                str(idx),
                item['product_code'],
                item['product_name'],
                f"{item['quantity']:.2f}",
                f"₹{item['price']:.2f}",
                f"₹{item['amount']:.2f}"
            ])
        
        # Add total row
        data.append(['', '', '', '', 'Total:', f"₹{invoice['total_amount']:.2f}"])
        
        # Create and style the table
        table = Table(data)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, -1), (-1, -1), colors.beige),
            ('TEXTCOLOR', (0, 1), (-1, -1), colors.black),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, -1), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
            ('BOX', (0, 0), (-1, -1), 2, colors.black),
            ('ALIGN', (3, 1), (-1, -1), 'RIGHT'),  # Right align numbers
        ]))
        
        elements.append(table)
        doc.build(elements)
        
        buffer.seek(0)
        response = make_response(buffer.getvalue())
        response.headers['Content-Type'] = 'application/pdf'
        response.headers['Content-Disposition'] = f'attachment; filename=invoice_{invoice_id}.pdf'
        
        return response
        
    except Exception as e:
        print(f"Error generating PDF: {str(e)}")
        return 'Error generating PDF', 500
    finally:
        if conn:
            conn.close()

@app.route('/products/<int:id>/stock', methods=['POST'])
def update_product_stock(id):
    try:
        product = Product.query.get_or_404(id)
        data = request.json
        change = data.get('change', 0)
        
        # Update stock
        product.stock += change
        db.session.commit()
        
        return jsonify({
            'success': True,
            'new_stock': product.stock
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({
            'success': False,
            'error': str(e)
        })

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        totp_token = request.form.get('totp_token')
        
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            if user.totp_enabled:
                if not totp_token:
                    flash('2FA token required')
                    return redirect(url_for('login'))
                
                totp = TOTP(user.totp_secret)
                if not totp.verify(totp_token):
                    flash('Invalid 2FA token')
                    return redirect(url_for('login'))
            
            session['user_id'] = user.id
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password')
            return redirect(url_for('login'))
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.before_request
def require_login():
    # List of routes that don't require login
    public_routes = ['login', 'static']
    
    # Check if the requested endpoint is in public routes
    if request.endpoint and request.endpoint not in public_routes:
        if 'logged_in' not in session:
            return redirect(url_for('login'))

@app.route('/users')
@admin_required
def users():
    users_list = User.query.all()
    return render_template('users.html', users=users_list)

@app.route('/users', methods=['POST'])
@admin_required
def add_user():
    data = request.get_json()
    
    if User.query.filter_by(username=data['username']).first():
        return jsonify({'error': 'Username already exists'}), 400
    
    hashed_password = generate_password_hash(data['password'])
    new_user = User(
        username=data['username'],
        password=hashed_password,
        role=data['role'],
        totp_enabled=data.get('totp_enabled', False),
        totp_secret=data.get('totp_secret') if data.get('totp_enabled') else None
    )
    
    try:
        db.session.add(new_user)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/users/<int:id>', methods=['PUT'])
@admin_required
def update_user(id):
    user = User.query.get_or_404(id)
    data = request.get_json()
    
    if data['username'] != user.username and User.query.filter_by(username=data['username']).first():
        return jsonify({'error': 'Username already exists'}), 400
    
    try:
        user.username = data['username']
        user.role = data['role']
        if data.get('password'):
            user.password = generate_password_hash(data['password'])
        
        # Handle 2FA changes
        user.totp_enabled = data.get('totp_enabled', False)
        if user.totp_enabled:
            user.totp_secret = data.get('totp_secret')
        else:
            user.totp_secret = None
        
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/users/<int:id>', methods=['DELETE'])
@admin_required
def delete_user(id):
    if id == session.get('user_id'):
        return jsonify({'error': 'Cannot delete your own account'}), 400
    
    user = User.query.get_or_404(id)
    try:
        db.session.delete(user)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@app.route('/calculator')
def calculator():
    settings = Settings.query.first()
    calculator_code = settings.calculator_code if settings else '9999'
    print(f"Loading calculator page with code: {calculator_code}")  # Debug log
    return render_template('calculator.html', calculator_code=calculator_code)

@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js')

@app.route('/api/slow_moving_products')
@cache.cached(timeout=300)
def slow_moving_products():
    try:
        thirty_days_ago = datetime.now() - timedelta(days=30)
        products = Product.query.all()
        slow_moving = []
        
        for product in products:
            last_sale = db.session.query(Invoice.date).join(InvoiceItem).filter(
                InvoiceItem.product_id == product.id
            ).order_by(Invoice.date.desc()).first()
            
            if last_sale:
                days_since_sale = (datetime.now().date() - last_sale[0]).days
            else:
                days_since_sale = 30  # Default for products with no sales
                
            if days_since_sale >= 30:
                slow_moving.append({
                    'name': product.description,
                    'days': days_since_sale
                })
        
        # Sort by days and get top 10
        slow_moving.sort(key=lambda x: x['days'], reverse=True)
        slow_moving = slow_moving[:10]
        
        return jsonify({
            'labels': [item['name'] for item in slow_moving],
            'values': [item['days'] for item in slow_moving]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/stock_sales_ratio')
@cache.cached(timeout=300)
def stock_sales_ratio():
    try:
        # Calculate daily stock-to-sales ratio for the last 30 days
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=29)
        dates = []
        ratios = []
        
        current_date = start_date
        while current_date <= end_date:
            # Get total stock for the day
            total_stock = db.session.query(func.sum(Product.stock)).scalar() or 0
            
            # Get total sales for the day
            daily_sales = db.session.query(func.sum(InvoiceItem.quantity)).join(Invoice).filter(
                func.date(Invoice.date) == current_date
            ).scalar() or 1  # Use 1 to avoid division by zero
            
            ratio = total_stock / daily_sales
            dates.append(current_date.strftime('%Y-%m-%d'))
            ratios.append(round(ratio, 2))
            
            current_date += timedelta(days=1)
        
        return jsonify({
            'labels': dates,
            'values': ratios
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/inventory_aging')
@cache.cached(timeout=300)
def inventory_aging():
    try:
        products = Product.query.all()
        aging_buckets = {
            '0-30 days': 0,
            '31-60 days': 0,
            '61-90 days': 0,
            '90+ days': 0
        }
        
        for product in products:
            last_sale = db.session.query(Invoice.date).join(InvoiceItem).filter(
                InvoiceItem.product_id == product.id
            ).order_by(Invoice.date.desc()).first()
            
            if last_sale:
                days_since_sale = (datetime.now().date() - last_sale[0]).days
            else:
                days_since_sale = 90  # Default for products with no sales
            
            if days_since_sale <= 30:
                aging_buckets['0-30 days'] += 1
            elif days_since_sale <= 60:
                aging_buckets['31-60 days'] += 1
            elif days_since_sale <= 90:
                aging_buckets['61-90 days'] += 1
            else:
                aging_buckets['90+ days'] += 1
        
        return jsonify({
            'labels': list(aging_buckets.keys()),
            'values': list(aging_buckets.values())
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/sales_forecast')
@cache.cached(timeout=300)
def sales_forecast():
    try:
        # Get historical daily sales for the last 90 days
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=89)
        
        daily_sales = db.session.query(
            func.date(Invoice.date).label('date'),
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            Invoice.date >= start_date,
            Invoice.date <= end_date
        ).group_by(
            func.date(Invoice.date)
        ).order_by(
            func.date(Invoice.date)
        ).all()
        
        # Create a simple moving average forecast
        sales_data = {row.date.strftime('%Y-%m-%d'): float(row.total) for row in daily_sales}
        dates = []
        actual_values = []
        forecast_values = []
        
        # Calculate moving average
        window_size = 7
        moving_avg = []
        
        # Fill historical data
        current_date = start_date
        while current_date <= end_date:
            date_str = current_date.strftime('%Y-%m-%d')
            dates.append(date_str)
            actual_values.append(sales_data.get(date_str, 0))
            
            # Calculate moving average for the last window_size days
            if len(actual_values) >= window_size:
                avg = sum(actual_values[-window_size:]) / window_size
                moving_avg.append(avg)
            else:
                moving_avg.append(None)
            
            current_date += timedelta(days=1)
        
        # Generate forecast for next 30 days
        last_avg = moving_avg[-1] if moving_avg else 0
        for i in range(30):
            forecast_date = end_date + timedelta(days=i+1)
            dates.append(forecast_date.strftime('%Y-%m-%d'))
            actual_values.append(None)
            forecast_values.append(last_avg)
        
        return jsonify({
            'labels': dates[-30:],  # Show only last 30 days
            'actual_values': actual_values[-30:],
            'forecast_values': forecast_values
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/sales_performance')
@login_required
def sales_performance():
    try:
        today = datetime.now().date()
        
        # Calculate daily sales
        daily_sales = db.session.query(
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            func.date(Invoice.date) == today
        ).scalar() or 0
        
        # Calculate weekly sales (last 7 days)
        week_start = today - timedelta(days=6)
        weekly_sales = db.session.query(
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            Invoice.date >= week_start,
            Invoice.date <= today
        ).scalar() or 0
        
        # Calculate monthly sales
        month_start = today.replace(day=1)
        monthly_sales = db.session.query(
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            Invoice.date >= month_start,
            Invoice.date <= today
        ).scalar() or 0
        
        # Calculate yearly sales
        year_start = today.replace(month=1, day=1)
        yearly_sales = db.session.query(
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            Invoice.date >= year_start,
            Invoice.date <= today
        ).scalar() or 0
        
        return jsonify({
            'daily': round(daily_sales, 2),
            'weekly': round(weekly_sales, 2),
            'monthly': round(monthly_sales, 2),
            'yearly': round(yearly_sales, 2)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/sales_growth')
@login_required
def sales_growth():
    try:
        today = datetime.now().date()
        
        # Day over Day (DoD) Growth
        yesterday = today - timedelta(days=1)
        today_sales = db.session.query(
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            func.date(Invoice.date) == today
        ).scalar() or 0
        
        yesterday_sales = db.session.query(
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            func.date(Invoice.date) == yesterday
        ).scalar() or 0
        
        dod_growth = ((today_sales - yesterday_sales) / yesterday_sales * 100) if yesterday_sales > 0 else 0
        
        # Month over Month (MoM) Growth
        current_month = today.replace(day=1)
        last_month = (current_month - timedelta(days=1)).replace(day=1)
        
        current_month_sales = db.session.query(
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            Invoice.date >= current_month,
            Invoice.date <= today
        ).scalar() or 0
        
        last_month_sales = db.session.query(
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            Invoice.date >= last_month,
            Invoice.date < current_month
        ).scalar() or 0
        
        mom_growth = ((current_month_sales - last_month_sales) / last_month_sales * 100) if last_month_sales > 0 else 0
        
        # Year over Year (YoY) Growth
        current_year = today.replace(month=1, day=1)
        last_year = current_year.replace(year=current_year.year-1)
        
        current_year_sales = db.session.query(
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            Invoice.date >= current_year,
            Invoice.date <= today
        ).scalar() or 0
        
        last_year_sales = db.session.query(
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            Invoice.date >= last_year,
            Invoice.date < current_year
        ).scalar() or 0
        
        yoy_growth = ((current_year_sales - last_year_sales) / last_year_sales * 100) if last_year_sales > 0 else 0
        
        return jsonify({
            'dod_growth': round(dod_growth, 2),
            'mom_growth': round(mom_growth, 2),
            'yoy_growth': round(yoy_growth, 2)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/sales_trend_by_period')
@login_required
def sales_trend_by_period():
    try:
        period = request.args.get('period', 'daily')  # daily, weekly, monthly, yearly
        end_date = datetime.now().date()
        
        if period == 'daily':
            start_date = end_date - timedelta(days=29)  # Last 30 days
            date_format = '%Y-%m-%d'
            date_trunc = func.date(Invoice.date)
        elif period == 'weekly':
            start_date = end_date - timedelta(weeks=11)  # Last 12 weeks
            date_format = '%Y-W%W'
            date_trunc = func.date_trunc('week', Invoice.date)
        elif period == 'monthly':
            start_date = end_date - timedelta(days=365)  # Last 12 months
            date_format = '%Y-%m'
            date_trunc = func.date_trunc('month', Invoice.date)
        else:  # yearly
            start_date = end_date.replace(year=end_date.year-4)  # Last 5 years
            date_format = '%Y'
            date_trunc = func.date_trunc('year', Invoice.date)
        
        sales_data = db.session.query(
            date_trunc.label('date'),
            func.sum(Invoice.total_amount).label('total')
        ).filter(
            Invoice.date >= start_date,
            Invoice.date <= end_date
        ).group_by(
            date_trunc
        ).order_by(
            date_trunc
        ).all()
        
        return jsonify({
            'labels': [row.date.strftime(date_format) for row in sales_data],
            'values': [float(row.total) for row in sales_data]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    init_db()  # Initialize database with sample data
    app.run(host='0.0.0.0', port=5000, debug=True)