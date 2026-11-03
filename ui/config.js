// Public settings for the web interface. Both values are safe to publish:
// data is protected by login + the allowed_emails list in the database.
window.SCOUT_CONFIG = {
  supabaseUrl: "https://YOUR-PROJECT.supabase.co",
  supabasePublishableKey: "sb_publishable_REPLACE_ME", // or the legacy "anon" key
};
