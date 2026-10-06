# Staff Onboarding (loan_officer / admin)

How a new Loan Officer or Administrator gets an account, and how an admin
resets a staff password.

Customers sign themselves up in the web app; that path only ever creates
`customer` accounts. Staff accounts are always created by an administrator,
and every staff member sets up two-factor login (an authenticator app) the
first time they sign in, exactly as customers do.

- **Web app:** https://primesvault.vercel.app
- **API docs (Swagger):** https://itp326png1checkpointcms-production.up.railway.app/api/docs
  — there is no Administrator screen in the web app yet, so admins create
  accounts and reset passwords here.

## 1. An admin creates the account

### Signing in to Swagger

Swagger needs an admin access token:

1. Open the API docs and expand **auth → POST /auth/login**. Choose **Try it
   out**, enter your admin email and password, and **Execute**. The response
   contains an `mfa_challenge_token`.
2. Click **Authorize** (top right), enter `Bearer ` followed by that
   `mfa_challenge_token`, and close the dialog.
3. Expand **POST /auth/mfa/verify-login**, enter the 6-digit code from your
   authenticator app as `code`, and **Execute**. The response contains your
   `access_token`.
4. Click **Authorize** again, **Logout**, then enter `Bearer ` followed by the
   `access_token`. It's valid for one hour.

### Creating the account

1. Expand **admin → POST /admin/staff**, choose **Try it out**, and fill in:
   - `email` — their work email (this is their username)
   - `full_name`
   - `role` — `loan_officer` or `admin` (`customer` is refused)
   - optionally `phone_number`; leave `is_active` as `true`
2. **Execute.** The response contains the new account and a
   `temporary_password`.
3. **Copy the temporary password now.** It is shown in this one response
   only: it isn't stored in readable form, isn't logged, and no other screen
   or endpoint will show it again. If it's lost, reset the password
   (section 3).

The action is recorded in the audit log as `staff_account_created`, with you
as the actor. The password is never recorded.

### Handing it over

Give the new staff member their email and temporary password over a private
channel: a password-manager share, a phone call, or in person. Never by
plain email or chat. Tell them to have an authenticator app ready (Google
Authenticator, Microsoft Authenticator, Authy or 1Password).

There is no way for staff to change their own password yet, so the
temporary password stays their password until an admin resets it.

### Fallback: the first admin

Creating staff in Swagger needs an admin to sign in. To create the **first**
admin, or if no admin can sign in, the dev team runs
`scripts/seed_staff.py` against the production database instead. It creates
the same kind of account, prints a temporary password once, and records a
`staff_account_created` audit entry (with no actor, and the operator's name
in `details.created_by`). From a machine with the Railway CLI linked to this
project:

```bash
railway run --service ITP326PNG1_CHECKPOINT_CMS \
  python scripts/seed_staff.py \
  --email jane@primesvault.pg \
  --full-name "Jane Officer" \
  --role admin \
  --created-by "your.name@primesvault.pg"
```

The script shows the database it's about to write to and asks for
confirmation; check it names the Supabase production host before typing
`yes`. Then hand over the password and continue with section 2, exactly as
for an account made in Swagger.

## 2. The new staff member signs in and sets up two-factor login

Send them these steps. Everything happens in the web app.

1. **Go to https://primesvault.vercel.app and choose Log in.** Enter your work
   email and the temporary password you were given.
2. **Set up two-factor login.** Because this is your first sign-in, the app
   says your account hasn't finished two-factor setup and shows a QR code.
   Open your authenticator app, add a new account, and scan the code. If you
   can't scan it, open **Can't scan the code?** and type the key shown into
   your app instead.
3. **Confirm it works.** Enter the 6-digit code your authenticator app now
   shows for PRIMESTONE, and choose **Verify and enable MFA**.
4. **Save your backup codes.** The app shows ten one-time backup codes, once
   only. Store them somewhere safe and private (a password manager is
   ideal). Each one lets you sign in once if you lose your phone. Then
   choose **I've saved my backup codes**.
5. **Sign in again.** You're taken back to the login form. Enter your email
   and password, then the current 6-digit code from your authenticator app.
   You land in the staff area, where your role (Loan Officer or Admin)
   decides what you can see and do.

From then on, every sign-in is email, password, then a code from the
authenticator app. If the phone isn't available, choose **Use a backup code
instead** on the code screen.

If they close the browser part-way through setup, nothing is lost: signing
in again with the temporary password restarts setup with a fresh QR code.

### Checking it worked (optional, admin)

In Swagger, **reports → GET /reports/audit-logs** with `actor_id` set to the
new account's id shows the sign-up sequence: `login_mfa_setup_required` →
`mfa_setup_initiated` → `mfa_enabled` → `login_password_verified` →
`login_success`.

## 3. Resetting a staff password

Use this when a staff member has forgotten their password, or you think
someone else may know it.

1. Sign in to Swagger as an admin (section 1).
2. Expand **admin → POST /admin/staff/{user_id}/reset-password**, enter the
   staff member's account id (shown when the account was created, and in
   the audit log), and **Execute**.
3. Copy the new `temporary_password` from the response. As with a new
   account, it is shown once only. Hand it over privately.

What a reset does:

- The old password stops working immediately.
- The staff member is **signed out everywhere**: every session they had
  open, on any device, ends.
- Their two-factor setup is **kept**. They sign in with the new password and
  the same authenticator app (or a backup code). They don't set it up again.
  If they never finished setting it up, they're asked to on their next
  sign-in, as in section 2.

Resets work only on staff accounts (`loan_officer` / `admin`), not
customers. Each one is recorded in the audit log as `staff_password_reset`,
with you as the actor. The password is never recorded.

## Known limitations

- **No Administrator screens yet.** Creating staff and resetting passwords
  is done in Swagger until the web app has an Administrator area.
- **No self-service password change.** Staff can't change their own
  password; an admin resets it. Customers have no password reset at all
  yet.
- **No way to deactivate an account yet.** A reset signs someone out, but
  to keep a person out for good (for example, they've left), the dev team
  sets `users.is_active = false` in the database. Deactivated accounts can't
  sign in.
- **Lost authenticator and no backup codes.** There is no admin action to
  clear someone's two-factor setup yet; it needs the dev team to reset it in
  the database.
