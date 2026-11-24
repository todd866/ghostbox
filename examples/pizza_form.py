"""Task: fill in and submit the public httpbin.org sample order form (echoes what you send; nothing ordered).

  python3 run.py --goal-file examples/pizza_form.py
"""

GOAL = "Fill in and submit the sample pizza order form at https://httpbin.org/forms/post."

MAX_STEPS = 40

GUIDE = """Use action 'navigate' to https://httpbin.org/forms/post

Fill the form, one field at a time (click the field, then type on the next turn):
  Customer name:          Ghost Box
  Telephone:              555-0100
  E-mail address:         box@example.com
  Pizza Size:             Medium (click its radio button)
  Pizza Toppings:         Bacon and Mushroom (click both checkboxes)
  Delivery instructions:  Leave it at the door

Then click the "Submit order" button. Choose 'done' when the page shows a JSON response containing
"custname": "Ghost Box". This form only echoes the data back; there is no payment and nothing is
ordered, so no checkpoint is needed for the submit.
"""
