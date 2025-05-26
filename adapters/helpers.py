def get_assistant_id(
    email_id: str = None,
    phone_number: str = None,
    ) -> str:
    """
    Get the assistant id from the email id or phone number
    """
    if email_id:
        return ""
    elif phone_number:
        return ""
    else:
        raise ValueError("Either email_id or phone_number must be provided")
