from portal import start_portal
import customtkinter as ctk

def main():
    root = ctk.CTk()
    start_portal(root)  
    root.mainloop()

if __name__ == '__main__':
    main()
  

# These are probably not relevant to you
# if something goes wrong, try .\.venv\Scripts\activate.bat in the terminal

#  Credentials saved to file: [C:\Users\witbr\AppData\Roaming\gcloud\application_default_credentials.json]

# These credentials will be used by any library that requests Application Default Credentials (ADC).

# Quota project "vonnaoman" was added to ADC which can be used by Google client libraries for billing and quota. 
# Note that some services may still bill the project owning the resource.