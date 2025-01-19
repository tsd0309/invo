# Inventory Management System

A modern web-based inventory and invoicing system built with Python Flask and SQLite. This system allows you to manage products, create invoices, and track inventory in real-time.

## Features

- Product Management (CRUD operations)
- Invoice Generation
- Real-time Stock Tracking
- Dashboard with Key Metrics
- Printer-friendly Invoice Templates
- Modern and Responsive UI

## Requirements

- Python 3.6 or higher
- pip (Python package installer)

## Installation

1. Clone this repository:
```bash
git clone <repository-url>
cd inventory-system
```

2. Create a virtual environment (recommended):
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

## Running the Application

1. Start the Flask development server:
```bash
python app.py
```

2. Open your web browser and navigate to:
```
http://localhost:5000
```

## Usage

### Products Management

1. Click on "Products" in the navigation menu
2. Use the "Add New Product" button to create products
3. Edit or delete existing products using the action buttons

### Creating Invoices

1. Click on "New Invoice" in the navigation menu
2. Fill in the customer details
3. Add products to the invoice using the "Add Item" button
4. The system will automatically calculate totals
5. Click "Save Invoice" to generate the invoice

### Viewing and Printing Invoices

1. Click on "Invoices" in the navigation menu
2. Use the "View" button to see invoice details
3. Use the "Print" button to get a printer-friendly version

## Database

The application uses SQLite as its database. The database file (`inventory.db`) will be created automatically when you first run the application.

## Security

- The application includes CSRF protection
- SQL injection protection through SQLAlchemy
- Input validation and sanitization
- Error handling and logging

## Contributing

1. Fork the repository
2. Create a new branch for your feature
3. Commit your changes
4. Push to the branch
5. Create a new Pull Request

## License

This project is licensed under the MIT License - see the LICENSE file for details. 