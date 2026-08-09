-- Resets PaperBoat's demo data: wipes all trades, positions, and users.
-- Run this manually in the Supabase SQL editor when you want a clean slate.
--
-- After running this, every user gets a fresh row (and a fresh 10 SOL
-- starting balance, priced in USD at whatever SOL is trading at) the next
-- time they send /start or interact with the bot - get_or_create_user()
-- in database.py handles that automatically, no code changes needed here.
--
-- This is destructive and cannot be undone. Back up first if you want to
-- keep trade history for any reason.

truncate table trades restart identity cascade;
truncate table positions restart identity cascade;
truncate table users restart identity cascade;
