# BizFlow AI
### Your Business. Your Numbers. One Intelligent Assistant.

**BizFlow AI** is an intelligent business management platform designed to simplify everyday financial and operational tasks for small businesses and independent service providers.

It brings customer management, invoicing, payment tracking, expense monitoring, cash flow forecasting and AI-powered business insights together in one dashboard.

---

## 🚀 Key Features

### 📊 Business Dashboard
- Financial overview with key performance indicators.
- Income and expense visualizations.
- Outstanding invoice and payment alerts.
- Business health indicators.

### 👥 Student & Customer Management
- Add, update and manage customer records.
- Track customer-related financial activity.
- Maintain organized business information.

### 🧾 Invoice & Payment Management
- Create and manage invoices.
- Track payments and outstanding balances.
- View invoice details and printable invoices.
- Monitor overdue payments.

### 💰 Expense Tracking
- Record and monitor business expenses.
- Understand how expenses affect cash flow.
- Maintain financial records in one place.

### 📈 Cash Flow Forecasting
- Forecast future cash balances.
- Analyze expected income and expenses.
- Identify potential cash shortages.

### 🧪 Interactive Financial Simulation
- Adjust expected payment collection.
- Simulate delayed customer payments.
- Model additional monthly expenses.
- Explore revenue changes.
- Compare baseline and simulated financial outcomes.

### 🤖 Intelligent Business Assistant
- Ask questions about business finances.
- Receive contextual business insights.
- Get assistant responses using the built-in fallback when an OpenAI API key is not configured.

### ⚙️ Personalization & Security
- Light and dark themes.
- Customizable dashboard widgets.
- User-specific data isolation.
- CSRF protection for form submissions.

---

## 🛠️ Technology Stack

| Layer | Technologies |
|---|---|
| Backend | Python, Flask |
| Frontend | HTML, CSS, JavaScript |
| Charts | Chart.js |
| Database | SQLite, PostgreSQL |
| Cloud Database | Neon PostgreSQL |
| Deployment | Render |
| AI Integration | OpenAI API (optional) |
| Testing | Python unittest |

---

## 🏗️ System Architecture

```text
             USER
               |
               v
       Flask Web Application
               |
       +-------+--------+
       |                |
       v                v
  Frontend UI      Backend Modules
  HTML/CSS/JS      Flask / Python
       |                |
       |       +--------+---------+
       |       |        |         |
       |       v        v         v
       |    Finance  Forecast  AI Assistant
       |       |        |         |
       +-------+--------+---------+
                       |
                       v
                 Database Layer
                       |
                 +-----+------+
                 |            |
                 v            v
              SQLite       PostgreSQL
                           (Neon)
                       |
                       v
                Business Insights
                & Financial Charts
```

---

## 💻 Run Locally

### 1. Clone the repository

```bash
git clone https://github.com/Charmi2007/BizFlowAI.git
cd BizFlowAI
```

### 2. Create a virtual environment

**Windows PowerShell:**

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```powershell
pip install -r requirements.txt
```

### 4. Configure environment variables

Create a `.env` file using `.env.example` as a reference.

Configure a secure `SECRET_KEY`.

For optional AI integration, configure your own `OPENAI_API_KEY`.

The application can use SQLite locally when PostgreSQL is not configured.

### 5. Start the application

```powershell
.venv\Scripts\python -m flask --app app run --debug
```

Open:

**http://127.0.0.1:5000**

---

## ☁️ Deployment

BizFlow AI is deployed using:

- **Render:** Flask web application hosting.
- **Neon:** Managed PostgreSQL database.

### Production start command

```bash
gunicorn --bind 0.0.0.0:$PORT 'app:create_app()'
```

Configure the required production environment variables in Render. Keep credentials and API keys out of source control.

---

## 🧪 Testing

Run the local test suite:

```powershell
$env:DATABASE_URL = "sqlite"
.venv\Scripts\python -m unittest discover -s tests -t . -v
```

Run PostgreSQL integration tests after configuring the database connection:

```powershell
$env:BIZFLOW_RUN_POSTGRES_TESTS = "1"
.venv\Scripts\python -m unittest tests.test_postgres -v
Remove-Item Env:BIZFLOW_RUN_POSTGRES_TESTS
```

The project has passed **60 SQLite tests and 8 PostgreSQL integration tests** in the development verification run.

---

## 🎯 Project Objective

Small businesses often manage invoices, payments, expenses and financial planning through disconnected tools or manual processes.

BizFlow AI aims to bring these activities into one accessible platform, helping business owners understand their financial position, anticipate cash flow challenges and make more informed decisions.

---

## 🔮 Future Enhancements

- Advanced financial analytics and reporting.
- Automated payment reminders.
- More configurable forecasting scenarios.
- Enhanced AI-driven recommendations.
- Exportable financial reports.
- Additional business integrations.

---

## 👩‍💻 Developer

**Charmi**

GitHub: [@Charmi2007](https://github.com/Charmi2007)

---


---

**BizFlow AI — Your Business. Your Numbers. One Intelligent Assistant.**
