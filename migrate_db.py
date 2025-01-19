from app import app, db, User
from sqlalchemy import inspect

def migrate_database():
    with app.app_context():
        try:
            # Drop and recreate all tables with new columns
            db.drop_all()
            db.create_all()
            
            # Create admin user if none exists
            if not User.query.filter_by(role='admin').first():
                from werkzeug.security import generate_password_hash
                admin_user = User(
                    username='admin',
                    password=generate_password_hash('admin'),
                    role='admin',
                    totp_enabled=False
                )
                db.session.add(admin_user)
                db.session.commit()
            
            print("Migration completed successfully!")
            print("Default admin user created with:")
            print("Username: admin")
            print("Password: admin")
            print("2FA is disabled by default")
            
        except Exception as e:
            print(f"Error during migration: {str(e)}")
            return

if __name__ == '__main__':
    migrate_database() 