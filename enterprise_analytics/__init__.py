# enterprise_analytics/__init__.py
"""
Enterprise Analytics & AI System
Advanced analytics, predictive insights, and machine learning capabilities
"""

from flask import Blueprint, request, jsonify, render_template, session, current_app
import MySQLdb.cursors
import json
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

try:
    import pandas as pd
    import numpy as np
    import plotly.graph_objs as go
    import plotly.utils
    from sklearn.ensemble import RandomForestRegressor, IsolationForest
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import StandardScaler
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import mean_squared_error, r2_score
    _ML_AVAILABLE = True
except ImportError:
    _ML_AVAILABLE = False
    pd = np = go = plotly = None

analytics_bp = Blueprint('analytics', __name__)

class AdvancedAnalytics:
    """Advanced Analytics Engine with ML capabilities"""
    
    def __init__(self):
        self.models = {}
        self.scalers = {}
    
    def get_company_data(self, company_id, days_back=90):
        """Get comprehensive company data for analysis"""
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Get employee data
            cur.execute("""
                SELECT id, full_name, email, job_title, department, role,
                       hire_date, performance_rating, total_learning_hours,
                       courses_completed, last_active_at, status
                FROM company_users 
                WHERE company_id = %s AND status = 'active'
            """, (company_id,))
            employees = cur.fetchall()
            
            # Get learning progress data
            cur.execute("""
                SELECT elp.*, cu.department, cu.role, cu.job_title
                FROM employee_learning_progress elp
                JOIN company_users cu ON elp.user_id = cu.user_id
                WHERE elp.company_id = %s 
                AND elp.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            """, (company_id, days_back))
            learning_data = cur.fetchall()
            
            # Get performance reviews
            cur.execute("""
                SELECT epr.*, cu.department, cu.role
                FROM employee_performance_reviews epr
                JOIN company_users cu ON epr.employee_id = cu.user_id
                WHERE epr.company_id = %s
                AND epr.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            """, (company_id, days_back))
            performance_data = cur.fetchall()
            
            # Get goals data
            cur.execute("""
                SELECT eg.*, cu.department, cu.role
                FROM employee_goals eg
                JOIN company_users cu ON eg.employee_id = cu.user_id
                WHERE eg.company_id = %s
                AND eg.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            """, (company_id, days_back))
            goals_data = cur.fetchall()
            
            # Get analytics data
            cur.execute("""
                SELECT * FROM company_analytics 
                WHERE company_id = %s 
                AND date >= DATE_SUB(NOW(), INTERVAL %s DAY)
                ORDER BY date DESC
            """, (company_id, days_back))
            analytics_data = cur.fetchall()
            
            cur.close()
            
            return {
                'employees': employees,
                'learning': learning_data,
                'performance': performance_data,
                'goals': goals_data,
                'analytics': analytics_data
            }
        except Exception as e:
            return None
    
    def calculate_engagement_score(self, employee_data, learning_data):
        """Calculate employee engagement score using ML"""
        try:
            # Create feature matrix
            features = []
            for emp in employee_data:
                emp_learning = [l for l in learning_data if l['user_id'] == emp['id']]
                
                # Calculate features
                total_courses = len(emp_learning)
                completed_courses = len([l for l in emp_learning if l['status'] == 'completed'])
                avg_progress = np.mean([l['progress_percentage'] for l in emp_learning]) if emp_learning else 0
                total_time = sum([l['time_spent_minutes'] for l in emp_learning])
                
                # Days since last activity
                last_active = emp['last_active_at']
                days_inactive = 0
                if last_active:
                    days_inactive = (datetime.now() - last_active).days
                
                features.append([
                    total_courses,
                    completed_courses,
                    avg_progress,
                    total_time,
                    days_inactive,
                    emp['performance_rating'] or 0,
                    emp['total_learning_hours'] or 0
                ])
            
            if not features:
                return []
            
            # Normalize features
            scaler = StandardScaler()
            features_scaled = scaler.fit_transform(features)
            
            # Use clustering to identify engagement levels
            kmeans = KMeans(n_clusters=3, random_state=42)
            clusters = kmeans.fit_predict(features_scaled)
            
            # Map clusters to engagement levels (0=Low, 1=Medium, 2=High)
            cluster_centers = kmeans.cluster_centers_
            cluster_scores = np.mean(cluster_centers, axis=1)
            cluster_mapping = {i: rank for rank, i in enumerate(np.argsort(cluster_scores))}
            
            engagement_scores = []
            for i, emp in enumerate(employee_data):
                engagement_level = cluster_mapping[clusters[i]]
                engagement_scores.append({
                    'employee_id': emp['id'],
                    'employee_name': emp['full_name'],
                    'engagement_score': engagement_level,
                    'engagement_label': ['Low', 'Medium', 'High'][engagement_level],
                    'features': {
                        'total_courses': features[i][0],
                        'completed_courses': features[i][1],
                        'avg_progress': features[i][2],
                        'total_time_minutes': features[i][3],
                        'days_inactive': features[i][4]
                    }
                })
            
            return engagement_scores
        except Exception as e:
            return []
    
    def predict_performance_trends(self, company_id, employee_data, performance_data):
        """Predict future performance trends"""
        try:
            if len(performance_data) < 10:  # Need minimum data
                return None
            
            # Prepare data for ML model
            df = pd.DataFrame(performance_data)
            
            # Feature engineering
            df['review_month'] = pd.to_datetime(df['review_period_end']).dt.month
            df['review_quarter'] = pd.to_datetime(df['review_period_end']).dt.quarter
            df['days_since_hire'] = (pd.to_datetime(df['review_period_end']) - 
                                   pd.to_datetime([emp['hire_date'] for emp in employee_data 
                                                 if emp['id'] in df['employee_id'].values])).dt.days
            
            # Select features and target
            feature_cols = ['technical_skills_rating', 'soft_skills_rating', 
                          'leadership_rating', 'goal_achievement_rating',
                          'review_month', 'review_quarter']
            
            # Filter out rows with missing values
            df_clean = df.dropna(subset=feature_cols + ['overall_rating'])
            
            if len(df_clean) < 5:
                return None
            
            X = df_clean[feature_cols]
            y = df_clean['overall_rating']
            
            # Train model
            X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
            
            model = RandomForestRegressor(n_estimators=100, random_state=42)
            model.fit(X_train, y_train)
            
            # Make predictions
            y_pred = model.predict(X_test)
            r2 = r2_score(y_test, y_pred)
            
            # Feature importance
            feature_importance = dict(zip(feature_cols, model.feature_importances_))
            
            # Predict for current employees
            current_predictions = []
            for emp in employee_data:
                if emp['performance_rating']:
                    # Create feature vector for prediction
                    current_month = datetime.now().month
                    current_quarter = (current_month - 1) // 3 + 1
                    hire_date = emp['hire_date']
                    days_since_hire = (datetime.now().date() - hire_date).days if hire_date else 365
                    
                    features = [
                        emp['performance_rating'],  # technical_skills_rating
                        emp['performance_rating'],  # soft_skills_rating
                        emp['performance_rating'],  # leadership_rating
                        emp['performance_rating'],  # goal_achievement_rating
                        current_month,
                        current_quarter
                    ]
                    
                    predicted_rating = model.predict([features])[0]
                    current_predictions.append({
                        'employee_id': emp['id'],
                        'employee_name': emp['full_name'],
                        'current_rating': emp['performance_rating'],
                        'predicted_rating': round(predicted_rating, 2),
                        'trend': 'improving' if predicted_rating > emp['performance_rating'] else 'declining'
                    })
            
            return {
                'model_accuracy': round(r2, 3),
                'feature_importance': feature_importance,
                'predictions': current_predictions
            }
        except Exception as e:
            return None
    
    def detect_learning_anomalies(self, learning_data):
        """Detect anomalies in learning patterns"""
        try:
            if len(learning_data) < 20:
                return []
            
            # Prepare features for anomaly detection
            features = []
            for record in learning_data:
                features.append([
                    record['progress_percentage'] or 0,
                    record['time_spent_minutes'] or 0,
                    record['attempts_count'] or 0,
                    record['final_score'] or 0
                ])
            
            # Use Isolation Forest for anomaly detection
            iso_forest = IsolationForest(contamination=0.1, random_state=42)
            anomalies = iso_forest.fit_predict(features)
            
            # Identify anomalous records
            anomalous_records = []
            for i, record in enumerate(learning_data):
                if anomalies[i] == -1:  # Anomaly detected
                    anomalous_records.append({
                        'employee_id': record['user_id'],
                        'content_name': record['content_name'],
                        'anomaly_type': self.classify_anomaly(features[i]),
                        'details': {
                            'progress': record['progress_percentage'],
                            'time_spent': record['time_spent_minutes'],
                            'attempts': record['attempts_count'],
                            'score': record['final_score']
                        }
                    })
            
            return anomalous_records
        except Exception as e:
            return []
    
    def classify_anomaly(self, feature_vector):
        """Classify type of learning anomaly"""
        progress, time_spent, attempts, score = feature_vector
        
        if time_spent > 1000 and progress < 50:
            return "High time, low progress"
        elif attempts > 5 and score < 60:
            return "Multiple attempts, low score"
        elif progress > 90 and time_spent < 30:
            return "Suspiciously fast completion"
        else:
            return "Unusual learning pattern"
    
    def generate_skill_gap_analysis(self, company_id, employee_data):
        """Generate skill gap analysis"""
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Get skills matrix data
            cur.execute("""
                SELECT esm.*, cu.department, cu.role
                FROM employee_skills_matrix esm
                JOIN company_users cu ON esm.employee_id = cu.user_id AND esm.company_id = cu.company_id
                WHERE esm.company_id = %s
            """, (company_id,))
            
            skills_data = cur.fetchall()
            cur.close()
            
            if not skills_data:
                return None
            
            # Analyze skill gaps by department and role
            skill_analysis = {}
            
            for skill_record in skills_data:
                dept = skill_record['department'] or 'Unknown'
                role = skill_record['role']
                skill = skill_record['skill_name']
                current_level = skill_record['current_level']
                target_level = skill_record['target_level']
                
                if dept not in skill_analysis:
                    skill_analysis[dept] = {}
                
                if skill not in skill_analysis[dept]:
                    skill_analysis[dept][skill] = {
                        'employees': 0,
                        'avg_current_level': 0,
                        'avg_target_level': 0,
                        'gap_score': 0,
                        'roles': {}
                    }
                
                # Convert skill levels to numeric
                level_map = {'novice': 1, 'beginner': 2, 'intermediate': 3, 'advanced': 4, 'expert': 5}
                current_numeric = level_map.get(current_level, 1)
                target_numeric = level_map.get(target_level, 3)
                
                skill_analysis[dept][skill]['employees'] += 1
                skill_analysis[dept][skill]['avg_current_level'] += current_numeric
                skill_analysis[dept][skill]['avg_target_level'] += target_numeric
                
                if role not in skill_analysis[dept][skill]['roles']:
                    skill_analysis[dept][skill]['roles'][role] = {'count': 0, 'gap': 0}
                
                skill_analysis[dept][skill]['roles'][role]['count'] += 1
                skill_analysis[dept][skill]['roles'][role]['gap'] += (target_numeric - current_numeric)
            
            # Calculate averages and gaps
            for dept in skill_analysis:
                for skill in skill_analysis[dept]:
                    emp_count = skill_analysis[dept][skill]['employees']
                    skill_analysis[dept][skill]['avg_current_level'] /= emp_count
                    skill_analysis[dept][skill]['avg_target_level'] /= emp_count
                    skill_analysis[dept][skill]['gap_score'] = (
                        skill_analysis[dept][skill]['avg_target_level'] - 
                        skill_analysis[dept][skill]['avg_current_level']
                    )
            
            return skill_analysis
        except Exception as e:
            return None
    
    def create_learning_recommendations(self, company_id, employee_id):
        """Generate personalized learning recommendations using AI"""
        try:
            cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

            # Get employee profile
            cur.execute("""
                SELECT * FROM company_users 
                WHERE id = %s AND company_id = %s
            """, (employee_id, company_id))
            employee = cur.fetchone()
            
            if not employee:
                return None
            
            # Get employee's learning history
            cur.execute("""
                SELECT * FROM employee_learning_progress 
                WHERE user_id = %s AND company_id = %s
                ORDER BY created_at DESC
            """, (employee_id, company_id))
            learning_history = cur.fetchall()
            
            # Get employee's skills
            cur.execute("""
                SELECT * FROM employee_skills_matrix 
                WHERE employee_id = %s AND company_id = %s
            """, (employee_id, company_id))
            skills = cur.fetchall()
            
            # Get similar employees (same role/department)
            cur.execute("""
                SELECT cu.id, elp.content_name, elp.final_score
                FROM company_users cu
                JOIN employee_learning_progress elp ON cu.id = elp.user_id
                WHERE cu.company_id = %s 
                AND cu.id != %s
                AND (cu.role = %s OR cu.department = %s)
                AND elp.status = 'completed'
                AND elp.final_score >= 80
            """, (company_id, employee_id, employee['role'], employee['department']))
            similar_employee_courses = cur.fetchall()
            
            cur.close()
            
            # Generate recommendations
            recommendations = []
            
            # 1. Skill gap based recommendations
            for skill in skills:
                if skill['current_level'] != skill['target_level']:
                    level_map = {'novice': 1, 'beginner': 2, 'intermediate': 3, 'advanced': 4, 'expert': 5}
                    current = level_map.get(skill['current_level'], 1)
                    target = level_map.get(skill['target_level'], 3)
                    
                    if target > current:
                        recommendations.append({
                            'type': 'skill_gap',
                            'priority': 'high' if target - current > 2 else 'medium',
                            'skill': skill['skill_name'],
                            'reason': f"Bridge gap from {skill['current_level']} to {skill['target_level']}",
                            'recommended_courses': skill.get('recommended_courses', [])
                        })
            
            # 2. Peer-based recommendations
            completed_courses = [l['content_name'] for l in learning_history if l['status'] == 'completed']
            peer_courses = {}
            
            for course_record in similar_employee_courses:
                course = course_record['content_name']
                if course not in completed_courses:
                    if course not in peer_courses:
                        peer_courses[course] = {'count': 0, 'avg_score': 0}
                    peer_courses[course]['count'] += 1
                    peer_courses[course]['avg_score'] += course_record['final_score']
            
            # Calculate average scores and recommend top courses
            for course, data in peer_courses.items():
                if data['count'] >= 2:  # At least 2 peers completed
                    avg_score = data['avg_score'] / data['count']
                    recommendations.append({
                        'type': 'peer_success',
                        'priority': 'medium',
                        'course': course,
                        'reason': f"{data['count']} similar colleagues completed with avg score {avg_score:.1f}%",
                        'peer_count': data['count'],
                        'avg_score': avg_score
                    })
            
            # 3. Performance-based recommendations
            if employee['performance_rating'] and employee['performance_rating'] < 3.5:
                recommendations.append({
                    'type': 'performance_improvement',
                    'priority': 'high',
                    'reason': 'Performance rating below target - focus on core competencies',
                    'suggested_areas': ['Communication', 'Leadership', 'Technical Skills']
                })
            
            # Sort by priority
            priority_order = {'high': 3, 'medium': 2, 'low': 1}
            recommendations.sort(key=lambda x: priority_order.get(x['priority'], 0), reverse=True)
            
            return recommendations[:10]  # Return top 10 recommendations
        except Exception as e:
            return None

# Initialize analytics engine
analytics_engine = AdvancedAnalytics()

@analytics_bp.route('/analytics/dashboard/<int:company_id>')
def analytics_dashboard(company_id):
    """Advanced analytics dashboard"""
    # Check permissions
    if session.get('company_id') != company_id or session.get('company_role') not in ['company_admin', 'hr_manager']:
        return redirect(url_for('dashboard.dashboard'))
    
    # Get company data
    data = analytics_engine.get_company_data(company_id)
    if not data:
        flash('Unable to load analytics data', 'error')
        return redirect(url_for('dashboard.dashboard'))
    
    # Calculate engagement scores
    engagement_scores = analytics_engine.calculate_engagement_score(data['employees'], data['learning'])
    
    # Predict performance trends
    performance_predictions = analytics_engine.predict_performance_trends(
        company_id, data['employees'], data['performance']
    )
    
    # Detect learning anomalies
    learning_anomalies = analytics_engine.detect_learning_anomalies(data['learning'])
    
    # Generate skill gap analysis
    skill_gaps = analytics_engine.generate_skill_gap_analysis(company_id, data['employees'])
    
    # Create visualizations
    charts = create_analytics_charts(data, engagement_scores, performance_predictions)
    
    return render_template('analytics_dashboard.html',
                         company_id=company_id,
                         engagement_scores=engagement_scores,
                         performance_predictions=performance_predictions,
                         learning_anomalies=learning_anomalies,
                         skill_gaps=skill_gaps,
                         charts=charts,
                         data=data)

@analytics_bp.route('/analytics/employee/<int:employee_id>/recommendations')
def employee_recommendations(employee_id):
    """Get personalized learning recommendations for employee"""
    company_id = session.get('company_id')
    if not company_id:
        return jsonify({'error': 'Unauthorized'}), 401
    
    recommendations = analytics_engine.create_learning_recommendations(company_id, employee_id)
    
    if recommendations is None:
        return jsonify({'error': 'Unable to generate recommendations'}), 500
    
    return jsonify({
        'success': True,
        'recommendations': recommendations
    })

@analytics_bp.route('/analytics/api/engagement-trends/<int:company_id>')
def get_engagement_trends(company_id):
    """API endpoint for engagement trends data"""
    if session.get('company_id') != company_id:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        # Get engagement trends over time
        cur.execute("""
            SELECT DATE(created_at) as date,
                   COUNT(*) as total_activities,
                   AVG(progress_percentage) as avg_progress,
                   COUNT(CASE WHEN status = 'completed' THEN 1 END) as completions
            FROM employee_learning_progress 
            WHERE company_id = %s 
            AND created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY)
            GROUP BY DATE(created_at)
            ORDER BY date
        """, (company_id,))
        
        trends = cur.fetchall()
        cur.close()
        
        return jsonify({
            'success': True,
            'data': trends
        })
    except Exception as e:
        return jsonify({'error': 'Failed to retrieve trends'}), 500

@analytics_bp.route('/analytics/api/department-performance/<int:company_id>')
def get_department_performance(company_id):
    """API endpoint for department performance comparison"""
    if session.get('company_id') != company_id:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        cur.execute("""
            SELECT department,
                   COUNT(*) as employee_count,
                   AVG(performance_rating) as avg_performance,
                   AVG(total_learning_hours) as avg_learning_hours,
                   SUM(courses_completed) as total_courses_completed
            FROM company_users 
            WHERE company_id = %s AND status = 'active'
            AND department IS NOT NULL
            GROUP BY department
            ORDER BY avg_performance DESC
        """, (company_id,))
        
        dept_performance = cur.fetchall()
        cur.close()
        
        return jsonify({
            'success': True,
            'data': dept_performance
        })
    except Exception as e:
        return jsonify({'error': 'Failed to retrieve department performance'}), 500

@analytics_bp.route('/analytics/api/learning-roi/<int:company_id>')
def calculate_learning_roi(company_id):
    """Calculate ROI on learning investments"""
    if session.get('company_id') != company_id:
        return jsonify({'error': 'Unauthorized'}), 401
    
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)

        # Get learning costs and performance improvements
        cur.execute("""
            SELECT 
                SUM(cd.learning_budget_per_employee * c.current_employee_count) as total_investment,
                AVG(cu.performance_rating) as current_avg_performance,
                COUNT(DISTINCT elp.user_id) as employees_trained,
                AVG(elp.final_score) as avg_course_score
            FROM companies c
            LEFT JOIN company_departments cd ON c.id = cd.company_id
            LEFT JOIN company_users cu ON c.id = cu.company_id
            LEFT JOIN employee_learning_progress elp ON c.id = elp.company_id
            WHERE c.id = %s AND cu.status = 'active'
        """, (company_id,))
        
        roi_data = cur.fetchone()
        
        # Calculate estimated ROI (simplified calculation)
        if roi_data and roi_data['total_investment']:
            performance_improvement = (roi_data['current_avg_performance'] - 3.0) / 3.0  # Baseline 3.0
            estimated_productivity_gain = performance_improvement * 0.15  # 15% productivity per rating point
            estimated_value = roi_data['total_investment'] * (1 + estimated_productivity_gain)
            roi_percentage = (estimated_value - roi_data['total_investment']) / roi_data['total_investment'] * 100
            
            roi_data['estimated_roi_percentage'] = round(roi_percentage, 2)
            roi_data['estimated_value_generated'] = round(estimated_value, 2)
        
        cur.close()
        
        return jsonify({
            'success': True,
            'data': roi_data
        })
    except Exception as e:
        return jsonify({'error': 'Failed to calculate ROI'}), 500

def create_analytics_charts(data, engagement_scores, performance_predictions):
    """Create interactive charts for analytics dashboard"""
    charts = {}
    
    try:
        # Engagement Score Distribution
        if engagement_scores:
            engagement_labels = [score['engagement_label'] for score in engagement_scores]
            engagement_counts = {label: engagement_labels.count(label) for label in set(engagement_labels)}
            
            fig_engagement = go.Figure(data=[
                go.Pie(labels=list(engagement_counts.keys()), 
                      values=list(engagement_counts.values()),
                      hole=0.3)
            ])
            fig_engagement.update_layout(title="Employee Engagement Distribution")
            charts['engagement_pie'] = json.dumps(fig_engagement, cls=plotly.utils.PlotlyJSONEncoder)
        
        # Performance Trend
        if data['analytics']:
            dates = [record['date'].strftime('%Y-%m-%d') for record in data['analytics']]
            performance_scores = [record.get('employee_satisfaction_score', 0) for record in data['analytics']]
            
            fig_performance = go.Figure()
            fig_performance.add_trace(go.Scatter(
                x=dates, y=performance_scores,
                mode='lines+markers',
                name='Performance Score',
                line=dict(color='#1f77b4', width=3)
            ))
            fig_performance.update_layout(
                title="Performance Trends Over Time",
                xaxis_title="Date",
                yaxis_title="Score"
            )
            charts['performance_trend'] = json.dumps(fig_performance, cls=plotly.utils.PlotlyJSONEncoder)
        
        # Learning Hours by Department
        if data['employees']:
            dept_hours = {}
            for emp in data['employees']:
                dept = emp['department'] or 'Unknown'
                if dept not in dept_hours:
                    dept_hours[dept] = 0
                dept_hours[dept] += emp['total_learning_hours'] or 0
            
            fig_dept_hours = go.Figure(data=[
                go.Bar(x=list(dept_hours.keys()), y=list(dept_hours.values()))
            ])
            fig_dept_hours.update_layout(title="Learning Hours by Department")
            charts['dept_learning_hours'] = json.dumps(fig_dept_hours, cls=plotly.utils.PlotlyJSONEncoder)
        
    except Exception as e:
        pass  # Charts are optional
    
    return charts

@analytics_bp.route('/analytics/export/<int:company_id>')
def export_analytics(company_id):
    """Export analytics data"""
    if session.get('company_id') != company_id or session.get('company_role') not in ['company_admin', 'hr_manager']:
        return jsonify({'error': 'Unauthorized'}), 401
    
    format_type = request.args.get('format', 'json')
    
    # Get comprehensive analytics data
    data = analytics_engine.get_company_data(company_id)
    engagement_scores = analytics_engine.calculate_engagement_score(data['employees'], data['learning'])
    
    export_data = {
        'company_id': company_id,
        'export_date': datetime.now().isoformat(),
        'employees': data['employees'],
        'engagement_scores': engagement_scores,
        'learning_data': data['learning'],
        'performance_data': data['performance'],
        'analytics_summary': data['analytics']
    }
    
    if format_type == 'csv':
        # Convert to CSV format (simplified)
        import csv
        import io
        
        output = io.StringIO()
        
        # Export employee engagement data
        if engagement_scores:
            writer = csv.writer(output)
            writer.writerow(['Employee ID', 'Employee Name', 'Engagement Level', 'Total Courses', 'Completed Courses'])
            
            for score in engagement_scores:
                writer.writerow([
                    score['employee_id'],
                    score['employee_name'],
                    score['engagement_label'],
                    score['features']['total_courses'],
                    score['features']['completed_courses']
                ])
        
        return jsonify({
            'success': True,
            'data': output.getvalue(),
            'format': 'csv'
        })
    
    return jsonify({
        'success': True,
        'data': export_data,
        'format': 'json'
    })
