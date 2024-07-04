import tkinter as tk
from tkinter import simpledialog
from tkinter import font as tkfont  # for custom fonts
from tkinter import ttk
from ttkthemes import ThemedTk
import customtkinter as ctk
from ..vonlib.googledrive import authenticate_google_drive, upload_file_to_drive, save_to_drive



text_input = None  # Define the text input widget globally
root = None  # Define the root window globally

def start_portal(root):
    def on_submit(event=None):  # Accept an optional event argument     
    
        user_input = text_input.get("1.0", tk.END).rstrip('\n') # Get the text from the text widget

        # print("User input raw:", user_input)
        if user_input.strip():  # Ensure the input is not just empty or spaces
            save_to_drive(user_input)  # Save the input to a file
        else: # If the input is empty, print a message
            print("No non blank input provided.") 

        text_input.delete("1.0", tk.END)  # Clear the text area after submission
    
        text_input.focus()  # Optionally set focus back to the text widget
        return "break"  # Return 'break' to prevent the default behavior of the event in this case, to prevent the submission \n being added to the textfield

    def exit_application():
        root.destroy()  # This will close the application window

#I think I need to set up things like the text input widget before I define the callbacks from the buttons
# CustomTkinter Text widget
    gtk_options = {
    "fg_color": "white",
    "text_color": "black",
    "border_width": 2,
    "bg_color":"white",
    }

    text_input = ctk.CTkTextbox(root,   corner_radius=10,  border_color="blue", font=("Helvetica", 12), width=380, height=100, **gtk_options)
    text_input.pack(pady=20, padx=20)
    text_input.bind('<Control-Return>',  on_submit)  # Bind the Enter key to submi
    text_input.focus()  # Set focus to the text input box
   
    #root = tk.Tk()
    # root = ThemedTk(theme="arc")  # Choose from available themes
    # Use CustomTkinter to create the main window
  
    root.title("tell von")
    root.geometry('400x200')

# Define custom styles that complement the Arc theme
    custom_font = tkfont.Font(family="Helvetica", size=12)
    bg_color = '#f2f2f2'  # Light background color typical of the Arc theme
    fg_color = '#383a42'  # Dark foreground color for better contrast

    # Define the text input widget
    #text_input = tk.Text(root, font=custom_font, bg=bg_color, fg=fg_color, insertbackground=fg_color, height=3, width=40)
    #text_input = ttk.Entry(root, font=custom_font, width=50)
    #text_input.pack(pady=20)



    submit_button = ctk.CTkButton(root, text='Submit', command=on_submit, border_color="blue", **gtk_options)                        
    submit_button.pack(side=tk.LEFT, padx=(20, 10))
    submit_button.bind('<Control-Return>',on_submit)

    exit_button = ctk.CTkButton(root, text='Exit', command=exit_application, border_color="red", **gtk_options)
    exit_button.pack(side=tk.RIGHT, padx=(10, 20))
   
    return root  # Return the root window
 

#  I apologize for the confusion. In `CTk`, the `background` option is not directly supported for the `Entry` widget as it is in standard Tkinter. The theming in `CTk` is a bit different.

# You can change the background color of an `Entry` widget by modifying the style associated with it. Here's an example:

# ```python
# import tkinter as tk
# import ctk as ttk  # Import the themed tkinter

# root = tk.Tk()

# # Create a style
# style = ttk.Style()
# style.configure('TEntry', background='red')

# # Create a button
# button = ttk.Button(root, text="My Button")
# button.pack()

# # Set the activated state on the button
# button.state(['!disabled'])  # This will enable the button

# # Unset the activated state on the button
# button.state(['disabled'])  # This will disable the button

# # Create a text entry area with the style
# text_entry = ttk.Entry(root, style='TEntry')
# text_entry.pack()

# root.mainloop()
# ```

# In this code, a new style `TEntry` is created with a red background, and this style is applied to the `Entry` widget. This will set the background color of the text entry area to red.

# Please note that changing the style will affect all widgets that use this style. If you want to apply the style to a specific widget only, you can create a custom style that inherits from the original style and apply the custom style to the widget.

# I hope this helps! Let me know if you have any other questions. 😊
