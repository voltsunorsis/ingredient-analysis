from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from dotenv import load_dotenv
import os
from models import User, Admin, IngredientAnalysis as Analysis
from services.ocr_service import OCRService
from services.ingredient_service import IngredientService
from pymongo import MongoClient
from bson import ObjectId
import base64
from datetime import datetime
import logging
from functools import wraps
import pytesseract
import traceback
import re

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', os.urandom(24))

# MongoDB setup
MONGO_URI = os.getenv('MONGO_URI', 'mongodb://localhost:27017/')
client = MongoClient(MONGO_URI)
db = client.ingredient_analyzer

# Initialize models with database connection
user_model = User(db)
admin_model = Admin(db)
analysis_model = Analysis(db)

# Initialize services
try:
    print("Initializing OCR service...")
    ocr_service = OCRService()
    print("OCR service initialized successfully")
    
    print("\nInitializing Ingredient service...")
    ingredient_service = IngredientService()
    print("Ingredient service initialized successfully")
    print("Services initialization complete")
    
except Exception as e:
    print(f"Error initializing services: {str(e)}")
    print("Please make sure Ollama is running and the deepseek-llm model is installed")
    raise

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Login required decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/')
def home():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        try:
            print("Login attempt received")
            
            # Check request content type
            print("Content-Type:", request.content_type)
            
            # Try to get JSON data
            try:
                data = request.get_json()
                print("Received data:", data)
            except Exception as e:
                print("Error parsing JSON:", str(e))
                return jsonify({'success': False, 'message': 'Invalid JSON data'}), 400
            
            if not data:
                print("No JSON data received")
                return jsonify({'success': False, 'message': 'No data provided'}), 400
                
            username = data.get('username')
            password = data.get('password')
            
            print(f"Login attempt - Username: {username}")
            
            if not username or not password:
                print("Missing username or password")
                return jsonify({'success': False, 'message': 'Username and password are required'}), 400

            # Try regular user login first
            try:
                user = user_model.verify_user(username, password)
                if user:
                    session['user_id'] = str(user['_id'])
                    session['is_admin'] = False
                    print(f"User login successful: {username}")
                    return jsonify({'success': True, 'is_admin': False})
            except Exception as e:
                print(f"Error during user verification: {str(e)}")

            # Try admin login
            try:
                admin = admin_model.verify_admin(username, password)
                if admin:
                    session['user_id'] = str(admin['_id'])
                    session['is_admin'] = True
                    print(f"Admin login successful: {username}")
                    return jsonify({'success': True, 'is_admin': True})
            except Exception as e:
                print(f"Error during admin verification: {str(e)}")

            print(f"Login failed for user: {username}")
            return jsonify({'success': False, 'message': 'Invalid username or password'}), 401

        except Exception as e:
            print(f"Login error: {str(e)}")
            return jsonify({'success': False, 'message': 'An error occurred during login'}), 500

    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    user_id = session.get('user_id')
    is_admin = session.get('is_admin', False)
    
    try:
        # Get user analyses
        if is_admin:
            # For admin, get all analyses
            analyses = []
            users = list(user_model.collection.find())
            for user in users:
                user_analyses = analysis_model.get_user_analyses(str(user['_id']))
                for analysis in user_analyses:
                    if analysis:
                        serialized = serialize_analysis(analysis)
                        if serialized:
                            serialized['username'] = user.get('username', 'Unknown')
                            analyses.append(serialized)
        else:
            # For regular users, get only their analyses
            user_analyses = analysis_model.get_user_analyses(user_id)
            analyses = []
            for analysis in user_analyses:
                if analysis:
                    serialized = serialize_analysis(analysis)
                    if serialized:
                        analyses.append(serialized)
        
        # Sort analyses by creation date
        analyses.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        
        # Store in session for use in compare view
        session['analysis_history'] = analyses
        
        return render_template('dashboard.html', 
                             analyses=analyses,
                             is_admin=is_admin,
                             analysis_history=analyses)
                             
    except Exception as e:
        print(f"Dashboard error: {str(e)}")
        flash("Error loading dashboard data", "error")
        return render_template('dashboard.html', 
                             analyses=[],
                             is_admin=is_admin,
                             analysis_history=[])

@app.route('/analyze', methods=['GET', 'POST'])
@login_required
def analyze():
    if request.method == 'GET':
        return render_template('analyze.html')
        
    try:
        print("Starting analysis...")
        data = request.get_json()
        
        if not data:
            print("No data received")
            return jsonify({'success': False, 'error': 'No data received'})
            
        if 'type' not in data or 'content' not in data:
            print("Missing required fields")
            return jsonify({'success': False, 'error': 'Missing type or content field'})
        
        content_type = data.get('type')
        content = data.get('content')
        product_name = data.get('product_name', '').strip()
        
        print(f"Content type: {content_type}")
        print(f"Product name: {product_name}")
        
        if not product_name:
            product_name = 'Unnamed Product'

        # Extract text based on content type
        if content_type == 'text':
            print("Processing text input...")
            extracted_text = content.strip()
        elif content_type == 'image':
            print("Processing image input...")
            try:
                # Remove header of base64 image
                if 'base64,' in content:
                    print("Found base64 header, removing it...")
                    content = content.split('base64,')[1]
                
                print("Calling OCR service...")
                extracted_text = ocr_service.extract_text_from_base64(content)
                print(f"OCR Result: {extracted_text[:100]}...")
                
            except Exception as e:
                print(f"Image processing error: {str(e)}")
                import traceback
                traceback.print_exc()
                return jsonify({
                    'success': False, 
                    'error': f'Image processing failed: {str(e)}',
                    'traceback': traceback.format_exc()
                })
        else:
            print(f"Invalid content type: {content_type}")
            return jsonify({'success': False, 'error': 'Invalid content type'})

        if not extracted_text or len(extracted_text.strip()) < 3:
            print("No text extracted")
            return jsonify({'success': False, 'error': 'No text could be extracted from the input'})

        # Analyze ingredients
        try:
            print("Analyzing ingredients...")
            print(f"Input text: {extracted_text[:100]}...")
            
            # Process ingredients
            ingredients = process_ingredients(extracted_text)
            if not ingredients:
                print("No ingredients found")
                return jsonify({'success': False, 'error': 'No ingredients could be identified'})
            
            print(f"Found ingredients: {ingredients[:5]}...")
            
            # Analyze with OpenAI
            analysis_result = analyze_with_ai(ingredients)
            if not analysis_result:
                error_msg = "Failed to analyze ingredients"
                if "quota exceeded" in str(e).lower() or "insufficient_quota" in str(e).lower():
                    error_msg = (
                        "The AI service is currently unavailable due to API quota limits. "
                        "Please try again later or contact support for assistance."
                    )
                print("AI analysis failed:", error_msg)
                return jsonify({'success': False, 'error': error_msg})
            
            print("Analysis successful")
            print(f"Health score: {analysis_result.get('health_score')}")
            print(f"Categories: {list(analysis_result.get('ingredient_percentages', {}).keys())}")
            
            # Add product name to the result
            analysis_result['product_name'] = product_name
            
            # Save to database
            try:
                print("Saving to database...")
                user_id = session.get('user_id')
                
                # Ensure health score is in 0-10 range
                if analysis_result['health_score'] > 10:
                    analysis_result['health_score'] = analysis_result['health_score'] / 10
                
                analysis_id = analysis_model.save_analysis(user_id, extracted_text, analysis_result)
                
                if not analysis_id:
                    print("Failed to save to database")
                    return jsonify({'success': False, 'error': 'Failed to save analysis'})
                    
                print("Successfully saved to database")
                
            except Exception as e:
                print(f"Database error: {str(e)}")
                return jsonify({'success': False, 'error': 'Failed to save analysis'})

            return jsonify({
                'success': True,
                'product_name': product_name,
                'health_score': analysis_result['health_score'],
                'ingredients': analysis_result['ingredients'],
                'ingredient_percentages': analysis_result['ingredient_percentages']
            })

        except Exception as e:
            print(f"Analysis error: {str(e)}")
            import traceback
            traceback.print_exc()
            return jsonify({
                'success': False, 
                'error': 'Failed to analyze ingredients',
                'details': str(e),
                'traceback': traceback.format_exc()
            })

    except Exception as e:
        print(f"General error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'success': False, 
            'error': 'An unexpected error occurred',
            'details': str(e),
            'traceback': traceback.format_exc()
        })

@app.route('/analyze_with_ai', methods=['POST'])
@login_required
def analyze_with_ai():
    try:
        data = request.get_json()
        if not data or 'ingredients_text' not in data:
            return jsonify({'error': 'No ingredients text provided'}), 400
            
        ingredients_text = data['ingredients_text']
        if not ingredients_text:
            return jsonify({'error': 'Empty ingredients text'}), 400
            
        # Here we'll add OpenAI integration later
        # For now, just use our basic analyzer
        analyzer = IngredientService()
        result = analyzer.analyze_ingredients(ingredients_text)
        
        if not result.get('success', False):
            return jsonify({'error': f'Analysis failed: {result.get("error", "Unknown error")}'})
            
        # Save to database
        user_id = session.get('user_id')
        analysis_id = analysis_model.save_analysis(
            user_id=user_id,
            ingredients_text=ingredients_text,
            analysis_result=result
        )
        
        if not analysis_id:
            return jsonify({'error': 'Failed to save analysis'}), 500
            
        result['analysis_id'] = str(analysis_id)
        return jsonify(result)
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/history')
@login_required
def history():
    try:
        user_id = session.get('user_id')
        page = request.args.get('page', 1, type=int)
        per_page = 10
        skip = (page - 1) * per_page
        
        # Get analyses from database
        analyses = analysis_model.get_user_analyses(user_id, skip=skip, limit=per_page)
        
        # Process analyses for display
        processed_analyses = []
        for analysis in analyses:
            # Format created_at date
            if 'created_at' in analysis:
                created_at = analysis['created_at']
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                    except:
                        try:
                            created_at = datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%S')
                        except:
                            created_at = datetime.now()
                analysis['created_at'] = created_at.strftime('%B %d, %Y %I:%M %p')
            
            # Ensure we have ingredient percentages
            if 'ingredient_percentages' not in analysis:
                analysis['ingredient_percentages'] = {
                    'Natural': 0,
                    'Additives': 0,
                    'Preservatives': 0,
                    'Artificial Colors': 0,
                    'Highly Processed': 0
                }
            
            if 'health_score' not in analysis:
                analysis['health_score'] = 0
                
            if 'product_name' not in analysis:
                analysis['product_name'] = 'Unnamed Product'
                
            if 'ingredients_text' not in analysis:
                analysis['ingredients_text'] = 'No ingredients listed'
                
            processed_analyses.append(analysis)
        
        # Sort by created_at date
        processed_analyses.sort(key=lambda x: x.get('created_at', ''), reverse=True)
        
        # Check if there are more pages
        total_analyses = len(processed_analyses)
        has_next = total_analyses == per_page
        
        return render_template('history_new.html', 
                             analyses=processed_analyses,
                             page=page,
                             has_next=has_next)
                             
    except Exception as e:
        logger.error(f"Error loading history: {str(e)}")
        flash(f"Error loading history: {str(e)}", 'error')
        return redirect(url_for('dashboard'))

@app.route('/compare')
@login_required
def compare_page():
    try:
        user_id = session.get('user_id')
        analyses = analysis_model.get_user_analyses(user_id, skip=0, limit=100)
        
        # Process analyses for display
        processed_analyses = []
        for analysis in analyses:
            # Format created_at date
            if 'created_at' in analysis:
                created_at = analysis['created_at']
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                    except:
                        try:
                            created_at = datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%S')
                        except:
                            created_at = datetime.now()
                analysis['date'] = created_at.strftime('%Y-%m-%d %H:%M')
            else:
                analysis['date'] = datetime.now().strftime('%Y-%m-%d %H:%M')
            
            # Ensure we have ingredient percentages
            if 'ingredient_percentages' not in analysis:
                analysis['ingredient_percentages'] = {
                    'Natural': 0,
                    'Additives': 0,
                    'Preservatives': 0,
                    'Artificial Colors': 0,
                    'Highly Processed': 0
                }
                
            if 'health_score' not in analysis:
                analysis['health_score'] = 0
                
            if 'product_name' not in analysis:
                analysis['product_name'] = 'Unnamed Product'
                
            if 'ingredients_text' not in analysis:
                analysis['ingredients_text'] = 'No ingredients listed'
                
            processed_analyses.append(analysis)
        
        return render_template('compare.html', analyses=processed_analyses)
        
    except Exception as e:
        logger.error(f"Error in compare page: {str(e)}")
        flash(f"Error loading comparison page: {str(e)}", 'error')
        return redirect(url_for('dashboard'))

@app.route('/compare_analyses', methods=['POST'])
@login_required
def compare_analyses():
    try:
        data = request.get_json()
        analysis_ids = data.get('analysis_ids', [])
        
        if not analysis_ids:
            return jsonify({'success': False, 'message': 'No analyses selected'})
        
        if len(analysis_ids) != 2:
            return jsonify({'success': False, 'message': 'Please select exactly 2 products to compare'})
        
        analyses = []
        for analysis_id in analysis_ids:
            try:
                analysis = analysis_model.collection.find_one({'_id': ObjectId(analysis_id)})
                if analysis:
                    # Convert ObjectId to string for JSON serialization
                    analysis['_id'] = str(analysis['_id'])
                    if 'user_id' in analysis:
                        analysis['user_id'] = str(analysis['user_id'])
                    if 'created_at' in analysis:
                        analysis['created_at'] = analysis['created_at'].strftime('%Y-%m-%d %H:%M:%S')
                    
                    # Ensure we have ingredient percentages
                    if 'ingredient_percentages' not in analysis:
                        analysis['ingredient_percentages'] = {
                            'Natural': 0,
                            'Additives': 0,
                            'Preservatives': 0,
                            'Artificial Colors': 0,
                            'Highly Processed': 0
                        }
                    
                    # For backward compatibility with UI
                    analysis['ingredient_categories'] = analysis['ingredient_percentages']
                    
                    if 'health_score' not in analysis:
                        analysis['health_score'] = 0
                        
                    if 'product_name' not in analysis:
                        analysis['product_name'] = 'Unnamed Product'
                        
                    if 'ingredients_text' not in analysis:
                        analysis['ingredients_text'] = 'No ingredients listed'
                    
                    analyses.append(analysis)
            except Exception as e:
                print(f"Error processing analysis {analysis_id}: {str(e)}")
                continue
        
        if len(analyses) != 2:
            return jsonify({'success': False, 'message': 'Could not find both selected products'})
        
        return jsonify({
            'success': True,
            'analyses': analyses
        })
        
    except Exception as e:
        print(f"Error in compare_analyses: {str(e)}")
        return jsonify({'success': False, 'message': f'An error occurred while comparing analyses: {str(e)}'})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
@login_required
def admin():
    if not session.get('is_admin'):
        return redirect(url_for('dashboard'))
    
    try:
        # Get all users with their analyses
        users = list(user_model.collection.find())
        total_users = len(users)
        total_analyses = 0
        
        # Initialize ingredient statistics
        ingredient_stats = {
            'total_ingredients': 0,
            'natural_ingredients': 0,
            'additives': 0,
            'preservatives': 0,
            'artificial_colors': 0,
            'highly_processed': 0,
            'harmful_ingredients': 0,
            'total_products': 0,
            'avg_health_score': 0.0,
            'health_score_distribution': {
                'excellent': 0,  # 8-10
                'good': 0,      # 6-7.9
                'fair': 0,      # 5-5.9
                'poor': 0       # 0-4.9
            }
        }
        
        # Get analyses for each user
        user_data = []
        all_health_scores = []
        
        for user in users:
            user_id = str(user['_id'])
            
            # Get user's analyses
            analyses = list(analysis_model.collection.find({"user_id": user_id}).sort("created_at", -1))
            total_analyses += len(analyses)
            
            # Process each analysis for ingredient statistics
            user_ingredients = {
                'total': 0,
                'natural': 0,
                'additives': 0,
                'preservatives': 0,
                'artificial_colors': 0,
                'highly_processed': 0,
                'harmful': 0
            }
            
            user_health_scores = []
            
            for analysis in analyses:
                # Update product count
                ingredient_stats['total_products'] += 1
                
                # Get health score
                health_score = analysis.get('health_score', 0)
                user_health_scores.append(health_score)
                all_health_scores.append(health_score)
                
                # Update health score distribution
                if health_score >= 8:
                    ingredient_stats['health_score_distribution']['excellent'] += 1
                elif health_score >= 6:
                    ingredient_stats['health_score_distribution']['good'] += 1
                elif health_score >= 5:
                    ingredient_stats['health_score_distribution']['fair'] += 1
                else:
                    ingredient_stats['health_score_distribution']['poor'] += 1
                
                # Process ingredients
                ingredients = analysis.get('ingredients', [])
                for ingredient in ingredients:
                    category = ingredient.get('category', 'Unknown')
                    is_harmful = ingredient.get('is_harmful', False)
                    
                    # Update global stats
                    ingredient_stats['total_ingredients'] += 1
                    if category == 'Natural':
                        ingredient_stats['natural_ingredients'] += 1
                    elif category == 'Additives':
                        ingredient_stats['additives'] += 1
                    elif category == 'Preservatives':
                        ingredient_stats['preservatives'] += 1
                    elif category == 'Artificial Colors':
                        ingredient_stats['artificial_colors'] += 1
                    elif category == 'Highly Processed':
                        ingredient_stats['highly_processed'] += 1
                    
                    if is_harmful:
                        ingredient_stats['harmful_ingredients'] += 1
                    
                    # Update user stats
                    user_ingredients['total'] += 1
                    if category == 'Natural':
                        user_ingredients['natural'] += 1
                    elif category == 'Additives':
                        user_ingredients['additives'] += 1
                    elif category == 'Preservatives':
                        user_ingredients['preservatives'] += 1
                    elif category == 'Artificial Colors':
                        user_ingredients['artificial_colors'] += 1
                    elif category == 'Highly Processed':
                        user_ingredients['highly_processed'] += 1
                    
                    if is_harmful:
                        user_ingredients['harmful'] += 1
            
            # Calculate user's average health score
            avg_health_score = sum(user_health_scores) / len(user_health_scores) if user_health_scores else 0
            
            # Format user data
            user_info = {
                'id': user_id,
                'username': user['username'],
                'email': user['email'],
                'created_at': user['created_at'],
                'is_active': user.get('is_active', True),
                'analyses': [],
                'stats': {
                    'total_analyses': len(analyses),
                    'avg_health_score': avg_health_score,
                    'ingredients': user_ingredients
                }
            }
            
            # Format analyses
            for analysis in analyses:
                formatted_analysis = {
                    'id': str(analysis['_id']),
                    'created_at': analysis['created_at'],
                    'product_name': analysis.get('product_name', 'Unnamed Product'),
                    'ingredients_text': analysis['ingredients_text'],
                    'health_score': analysis.get('health_score', 0),
                    'ingredients': analysis.get('ingredients', []),
                    'ingredient_percentages': analysis.get('ingredient_percentages', {}),
                    'warnings': analysis.get('warnings', [])
                }
                user_info['analyses'].append(formatted_analysis)
            
            user_data.append(user_info)
        
        # Calculate overall average health score
        ingredient_stats['avg_health_score'] = sum(all_health_scores) / len(all_health_scores) if all_health_scores else 0
        
        # Calculate percentages for ingredient distribution
        total_ingredients = ingredient_stats['total_ingredients']
        if total_ingredients > 0:
            ingredient_stats['natural_percentage'] = (ingredient_stats['natural_ingredients'] / total_ingredients) * 100
            ingredient_stats['additives_percentage'] = (ingredient_stats['additives'] / total_ingredients) * 100
            ingredient_stats['preservatives_percentage'] = (ingredient_stats['preservatives'] / total_ingredients) * 100
            ingredient_stats['artificial_colors_percentage'] = (ingredient_stats['artificial_colors'] / total_ingredients) * 100
            ingredient_stats['highly_processed_percentage'] = (ingredient_stats['highly_processed'] / total_ingredients) * 100
            ingredient_stats['harmful_percentage'] = (ingredient_stats['harmful_ingredients'] / total_ingredients) * 100
        
        return render_template(
            'admin.html',
            users=user_data,
            stats={
                'total_users': total_users,
                'total_analyses': total_analyses,
                'avg_analyses_per_user': total_analyses / total_users if total_users > 0 else 0,
                'active_users': len([u for u in users if u.get('is_active', True)]),
                'ingredients': ingredient_stats
            }
        )
        
    except Exception as e:
        print(f"Admin page error: {str(e)}")
        flash("Error loading admin data", "error")
        return render_template('admin.html', users=[], stats={})

@app.route('/admin/user/<user_id>/analyses')
@login_required
def admin_user_analyses(user_id):
    if not session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 403
        
    try:
        analyses = list(analysis_model.collection.find({"user_id": user_id}).sort("created_at", -1))
        return jsonify({
            'analyses': [{
                'id': str(a['_id']),
                'created_at': a['created_at'].isoformat(),
                'product_name': a.get('product_name', 'Unnamed Product'),
                'health_score': a.get('health_score', 0),
                'ingredients_count': len(a.get('ingredients', [])),
                'harmful_ingredients': len([i for i in a.get('ingredients', []) if i.get('is_harmful', False)])
            } for a in analyses]
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/stats')
@login_required
def admin_stats():
    if not session.get('is_admin'):
        return jsonify({'error': 'Unauthorized'}), 403
        
    try:
        # Get overall statistics
        total_users = user_model.collection.count_documents({})
        total_analyses = analysis_model.collection.count_documents({})
        active_users = user_model.collection.count_documents({'is_active': True})
        
        # Get analysis trends (last 7 days)
        now = datetime.datetime.utcnow()
        week_ago = now - datetime.timedelta(days=7)
        daily_analyses = list(analysis_model.collection.aggregate([
            {
                '$match': {
                    'created_at': {'$gte': week_ago}
                }
            },
            {
                '$group': {
                    '_id': {
                        '$dateToString': {
                            'format': '%Y-%m-%d',
                            'date': '$created_at'
                        }
                    },
                    'count': {'$sum': 1}
                }
            },
            {'$sort': {'_id': 1}}
        ]))
        
        return jsonify({
            'total_users': total_users,
            'total_analyses': total_analyses,
            'active_users': active_users,
            'avg_analyses_per_user': total_analyses / total_users if total_users > 0 else 0,
            'daily_analyses': daily_analyses
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/stats')
@login_required
def api_admin_stats():
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Get stats from database
    try:
        # Placeholder data - replace with actual database queries
        stats = {
            'totalUsers': 10,
            'totalAnalyses': 50,
            'activeToday': 5
        }
        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/activity')
@login_required
def api_admin_activity():
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Get activity log from database
    try:
        # Placeholder data - replace with actual database queries
        activities = [
            {
                'timestamp': '2024-12-25T10:30:00',
                'username': 'user1',
                'action': 'Analysis',
                'details': 'Analyzed product ingredients'
            }
        ]
        return jsonify({'activities': activities})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/users')
@login_required
def api_admin_users():
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Get user list from database
    try:
        # Placeholder data - replace with actual database queries
        users = [
            {
                '_id': '1',
                'username': 'user1',
                'email': 'user1@example.com',
                'lastActive': '2024-12-25T10:30:00',
                'active': True
            }
        ]
        return jsonify({'users': users})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/admin/users/<user_id>', methods=['DELETE'])
@login_required
def delete_user(user_id):
    if not session.get('is_admin', False):
        return jsonify({'error': 'Unauthorized'}), 401
    
    # Delete user from database
    try:
        # Placeholder - replace with actual database operation
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/test_ocr', methods=['POST'])
def test_ocr():
    try:
        data = request.get_json()
        if not data or 'content' not in data:
            return jsonify({'success': False, 'error': 'No image data provided'})
        
        content = data.get('content')
        print("Received image data length:", len(content) if content else 0)
        
        try:
            # Remove header of base64 image if present
            if 'base64,' in content:
                print("Found base64 header, removing it...")
                content = content.split('base64,')[1]
            
            print("Testing OCR service...")
            print("Tesseract path:", pytesseract.pytesseract.tesseract_cmd)
            print("Tesseract exists:", os.path.exists(pytesseract.pytesseract.tesseract_cmd))
            
            extracted_text = ocr_service.extract_text_from_base64(content)
            print("Extracted text:", extracted_text[:100] if extracted_text else "No text extracted")
            
            return jsonify({
                'success': True,
                'text': extracted_text,
                'tesseract_path': pytesseract.pytesseract.tesseract_cmd,
                'tesseract_exists': os.path.exists(pytesseract.pytesseract.tesseract_cmd)
            })
            
        except Exception as e:
            print(f"OCR processing error: {str(e)}")
            import traceback
            traceback.print_exc()
            return jsonify({
                'success': False,
                'error': str(e),
                'traceback': traceback.format_exc(),
                'tesseract_path': pytesseract.pytesseract.tesseract_cmd,
                'tesseract_exists': os.path.exists(pytesseract.pytesseract.tesseract_cmd)
            })
            
    except Exception as e:
        print(f"Test route error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()})

def process_ingredients(text):
    """Process and clean ingredients text."""
    if not text:
        return []
        
    # Remove common prefixes
    text = re.sub(r'^ingredients:?\s*', '', text.lower(), flags=re.IGNORECASE)
    
    # Split ingredients by common delimiters
    ingredients = re.split(r'[,;.]', text)
    
    # Clean and filter ingredients
    cleaned_ingredients = []
    for ingredient in ingredients:
        # Clean the ingredient
        ingredient = ingredient.strip()
        ingredient = re.sub(r'\([^)]*\)', '', ingredient)  # Remove parentheses and their contents
        ingredient = re.sub(r'\s+', ' ', ingredient)  # Normalize whitespace
        
        # Skip if too short or empty
        if len(ingredient) < 2:
            continue
            
        # Skip if it's just numbers or symbols
        if re.match(r'^[\d\W]+$', ingredient):
            continue
            
        cleaned_ingredients.append(ingredient)
    
    return cleaned_ingredients

def analyze_with_ai(ingredients):
    """Analyze ingredients using the ingredient service."""
    try:
        # Join ingredients into a text string
        ingredients_text = ', '.join(ingredients)
        
        # Use the ingredient service to analyze
        result = ingredient_service.analyze_ingredients(ingredients_text)
        
        if not result:
            print("No result from ingredient service")
            return None
            
        return result
        
    except Exception as e:
        print(f"Error in analyze_with_ai: {str(e)}")
        return None

def serialize_mongo_doc(doc):
    """Convert MongoDB document to JSON-serializable format"""
    if isinstance(doc, dict):
        return {k: serialize_mongo_doc(v) for k, v in doc.items()}
    elif isinstance(doc, list):
        return [serialize_mongo_doc(item) for item in doc]
    elif isinstance(doc, ObjectId):
        return str(doc)
    elif isinstance(doc, datetime):
        return doc.strftime('%Y-%m-%d %H:%M:%S')
    else:
        return doc

def serialize_analysis(analysis):
    if not analysis:
        return None
    
    try:
        # Convert ObjectId to string
        analysis['_id'] = str(analysis['_id'])
        analysis['user_id'] = str(analysis['user_id'])
        
        # Convert datetime to string
        if 'created_at' in analysis:
            analysis['created_at'] = analysis['created_at'].strftime('%Y-%m-%d %H:%M:%S')
            
        # Ensure we have product name
        if 'product_name' not in analysis:
            analysis['product_name'] = 'Unnamed Product'
            
        # Handle undefined values in analysis_result
        if 'analysis_result' in analysis:
            result = analysis['analysis_result']
            if isinstance(result, dict):
                if 'ingredients' in result:
                    result['ingredients'] = [i for i in result['ingredients'] if i is not None]
                if 'health_score' not in result:
                    result['health_score'] = 0
                    
        return analysis
    except Exception as e:
        print(f"Error serializing analysis: {str(e)}")
        return None

def calculate_health_score(percentages):
    """Calculate health score based on ingredient percentages"""
    weights = {
        'Natural': 1.0,
        'Additives': -0.3,
        'Preservatives': -0.3,
        'Artificial Colors': -0.2,
        'Highly Processed': -0.4
    }
    
    score = sum(percentages[cat] * weights[cat] for cat in percentages)
    # Normalize to 0-10 range
    normalized_score = min(max(5 + score/20, 0), 10)
    return round(normalized_score, 1)

if __name__ == '__main__':
    # Check MongoDB connection and list users
    try:
        print("Checking MongoDB connection...")
        # List all users
        users = list(user_model.collection.find({}, {"username": 1, "_id": 0}))
        print("Existing users:", [user['username'] for user in users])
        
        # Check if user1 exists, if not create it
        user1 = user_model.collection.find_one({"username": "user1"})
        if not user1:
            print("Creating user1...")
            user_model.create_user("user1", "user1@example.com", "1")
            print("Created user1 - username: user1, password: 1")
        else:
            print("user1 already exists")
            
    except Exception as e:
        print(f"MongoDB Error: {str(e)}")
        
    app.run(host='0.0.0.0', port=5000, debug=True)