# Putting Cartel online

About an hour, once. After this the app lives on the internet, the data lives in
a database the group shares, and your laptop is just another device with a
browser.

Two free services, neither needing a credit card:

- **Supabase** holds the data. Think of it as a safe-deposit box for
  `cartel.db` — the same records, kept somewhere every copy of the app can
  reach.
- **Streamlit Cloud** runs the app and gives it a web address.

You will touch Supabase twice: once to create it, once to copy one long line of
text. After that you never open it again.

---

## Before you start

On your laptop, in the app: **Health → Back up now**. Then copy
`C:\Cartel\cartel-app\data\cartel.db` somewhere safe. You are about to move
four and a half years of golf.

---

# Part 1 — The database

### 1. Create the account

Go to **https://supabase.com** and click **Start your project**. Sign in with
GitHub if you already have an account — one login for both — or use an email
address.

It asks you to create an **organization** before your first project. Name it
anything; your own name is fine. Choose the **Free** plan.

### 2. Create the project

1. **New project**
2. Name it `cartel`
3. Set a database password and **write it down somewhere you won't lose it**
4. Region: **East US** is closest to Nashville
5. **Create new project**, then wait about two minutes while it builds

### 3. Copy the connection string

1. Click **Connect** at the top of the project page
2. Choose **Connection string → Transaction pooler**
3. Copy the whole line. It looks like:

```
postgresql://postgres.abcdefgh:PASSWORD@aws-0-us-east-1.pooler.supabase.com:6543/postgres
```

4. Replace the word `PASSWORD` with the password from step 2

Keep that line somewhere for the next few steps. Treat it like a house key: it
is the only thing standing between the internet and your records.

---

# Part 2 — The app's home

Streamlit's free hosting deploys from GitHub, so you need a GitHub account even
though you'll never write code there. It's the filing cabinet the app is served
from.

### 4. Create the GitHub account

**https://github.com** → **Sign up**. Email, password, username. The username
becomes part of your web address, so keep it plain. Verify the email, skip the
personalisation questions, choose the **Free** plan.

### 5. Make the repository

1. **+** at the top right → **New repository**
2. Name it `cartel`
3. Mark it **Private**
4. **Create repository**

### 6. Upload the app

Unzip **`cartel-for-github.zip`** somewhere. It contains only the program —
no database, no spreadsheets, no backups.

On the empty repository page click **uploading an existing file**, then drag in
everything from that unzipped folder. Scroll down, click **Commit changes**.

> Use the `cartel-for-github` package, not your `C:\Cartel\cartel-app` folder.
> The two differ in one important way: the hosted copy's `requirements.txt`
> includes the Postgres driver, which the laptop copy deliberately leaves out.
> Upload the laptop folder and the app will start and then fail the moment it
> reaches Supabase.

---

# Part 3 — Go live

### 7. Deploy

1. **https://share.streamlit.io** → **Sign in with GitHub**, approve the prompt
2. **Create app** → **Deploy a public app from a repo**
3. Repository: your `cartel`. Main file: `app.py`
4. **Before deploying**, open **Advanced settings → Secrets** and paste:

```toml
CARTEL_DB_URL = "postgresql://postgres.abcdefgh:yourpassword@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
```

5. **Deploy**

Two or three minutes later you have an address like `cartel.streamlit.app`.

Open it. Every tab will be empty, with a note saying nothing is loaded. That is
correct — the app is running, the database is empty. That's the next step.

### 8. Send your data up

On your laptop, in `C:\Cartel\cartel-app`, hold **Shift**, right-click on empty
space, **Open PowerShell window here**. Then:

```
.venv\Scripts\python.exe scripts\cartel_cli.py migrate --to "PASTE-YOUR-CONNECTION-STRING" --apply
```

Quotes included. It prints every table and checks the row counts on arrival:

```
   members             46 of 46     ok
   rounds             213 of 213    ok
   entries           2983 of 2983   ok
   ...
Done. Your local file is untouched - keep it as a backup.
```

Refresh the web address. Your standings should be there.

**Check the four Standings figures against your laptop.** They must match
exactly. If they do, the move worked.

### 9. Lock the four admin buttons

On the hosted app: **Health → The admin word → Set or change**. Pick something
the organisers will remember.

Until you do this, anyone with the link can change the stake or edit the roster.

### 10. Share it

Send the group the address. Tell them to add it to their home screen.

---

## After you go live

**Stop using the laptop copy.** It still works, and it still has the old data in
it — but a round settled there will never reach the hosted database, and the two
histories cannot be merged afterwards.

Rename `C:\Cartel\cartel-app\data\cartel.db` to `cartel-BEFORE-HOSTING.db`. The
app will then say it has no data, which is exactly the reminder you want.

From then on, **the web address is the app** — on your laptop as much as
anyone's phone.

---

## Two things to expect

**The app sleeps after 12 hours with no visitors.** Since you play twice a week,
it will usually be asleep when someone opens it. They'll see a page with a
button — *"Yes, get this app back up!"* — which anyone can click. About thirty
seconds. **Say this when you share the link**, or the first person to try will
report it as broken.

**A free Supabase database pauses after 7 days with no activity.** In season it
never happens. Over a long off-season it will, and the app won't work until you
click **Restore** on the Supabase dashboard. Nothing is lost. Expect it before
the first round of the year.

---

## Backups, now that it's hosted

The app's own backups switch off when the data is in Supabase — it says so on
the Health tab. Supabase runs its own.

I'd still take your own copy every month or two:

```
CARTEL_DB_URL=... .venv\Scripts\python.exe scripts\cartel_cli.py migrate --to "sqlite-path" --apply
```

Or simply keep the pre-hosting `cartel.db` you set aside. Free-tier backups are
not generous, and the whole history is under a megabyte.

---

## If something goes wrong

| What you see | What it means |
|---|---|
| App loads, every tab empty | The database is empty. Run step 8. |
| *"CARTEL_DB_URL points at Postgres but psycopg isn't installed"* | You uploaded the laptop folder rather than `cartel-for-github`. Replace `requirements.txt` in the repo and it will redeploy. |
| *"could not connect"* on migrate | Check the whole string is in quotes, that you replaced `PASSWORD`, and that the project isn't paused. |
| Figures don't match the laptop | Stop and tell me before anyone posts a round. |
| Page says the app is sleeping | Normal. Click the button, wait thirty seconds. |
