from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class CarryForwardAuthorizationTests(TransactionTestCase):
    """0009 must read the old ``visa_status`` column before dropping it."""

    before = [("accounts", "0008_profile_citizenship_profile_preferred_currency_and_more")]
    after = [("accounts", "0009_profile_per_country_authorization")]

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_existing_visa_status_is_carried_into_the_us_entry(self):
        old_apps = self._migrate(self.before)
        user = old_apps.get_model("auth", "User").objects.create(username="carry")
        old_apps.get_model("accounts", "Profile").objects.filter(user=user).delete()
        old_apps.get_model("accounts", "Profile").objects.create(user=user, visa_status="cpt")
        blank = old_apps.get_model("auth", "User").objects.create(username="blank")
        old_apps.get_model("accounts", "Profile").objects.filter(user=blank).delete()
        old_apps.get_model("accounts", "Profile").objects.create(user=blank)

        new_apps = self._migrate(self.after)
        Profile = new_apps.get_model("accounts", "Profile")
        self.assertEqual(Profile.objects.get(user_id=user.pk).visa_status_by_country, {"USA": "opt"})
        self.assertEqual(Profile.objects.get(user_id=blank.pk).visa_status_by_country, {})
