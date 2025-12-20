"""
This module defines the workflow for onboarding a new lab member.
"""


def create_university_email(member_name):
    """Creates a university email account for the new lab member."""
    print(f"Creating university email account for {member_name}...")


def add_to_slack(member_name):
    """Adds the new lab member to the lab's Slack workspace."""
    print(f"Adding {member_name} to Slack...")


def add_to_jira(member_name):
    """Adds the new lab member to the lab's Jira instance."""
    print(f"Adding {member_name} to Jira...")


def grant_campus_access(member_name):
    """Grants the new lab member access to the Newmarket campus."""
    print(f"Granting {member_name} access to Newmarket campus...")


def add_to_mailing_lists(member_name):
    """Adds the new lab member to the relevant mailing lists."""
    print(f"Adding {member_name} to mailing lists...")


def provision_computer(member_name):
    """Provisions a computer for the new lab member."""
    print(f"Provisioning a computer for {member_name}...")


def grant_server_access(member_name):
    """Grants the new lab member access to the lab's servers."""
    print(f"Granting {member_name} server access...")


def upload_project_description(member_name):
    """Uploads the new lab member's project description."""
    print(f"Uploading project description for {member_name}...")


def update_sail_website(member_name):
    """Creates a Jira task to update the SAIL website with the new lab member's information."""
    print(f"Creating Jira task to update SAIL website for {member_name}...")


def request_project_details(member_name):
    """Asks the new lab member for their project title, preferred name, and a picture."""
    print(f"Requesting project details from {member_name}...")


def run_onboarding_workflow(member_name):
    """Runs the full onboarding workflow for a new lab member."""
    create_university_email(member_name)
    add_to_slack(member_name)
    add_to_jira(member_name)
    grant_campus_access(member_name)
    add_to_mailing_lists(member_name)
    provision_computer(member_name)
    grant_server_access(member_name)
    upload_project_description(member_name)
    update_sail_website(member_name)
    request_project_details(member_name)
