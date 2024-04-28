import tkinter
import customtkinter
from pytube import YouTube


def startDownload():
    try:
        ytLink = link.get()
#       print("Downloading video from:", ytLink)
        ytObject = YouTube(ytLink, on_progress_callback = on_progress)
        video = ytObject.streams.get_highest_resolution()

        title.configure(text=ytObject.title, text_color="white")
        finishLabel.configure(text="")
        video.download()
#        download_path = os.path.join(os.path.expanduser('~'), 'Downloads')
#        print("Download path:", download_path)
#        if not os.access(download_path, os.W_OK):
#            print("Download directory may not have write permissions. Try running the script with 'sudo'.")
#            return
#        video.download(output_path=download_path)
        finishLabel.configure(text="Downloaded!", text_color="green")
    except Exception as e:
        finishLabel.configure(text="Something's wrong!", text_color="red")


def on_progress(stream, chunk, bytes_remaining):
    total_size = stream.filesize
    bytes_downloaded = total_size - bytes_remaining
    percentage_of_completion = bytes_downloaded / total_size * 100
    per = str(int(percentage_of_completion))
    pPercentage.configure(text=per + '%')
    pPercentage.update()

    progressBar.set(float(percentage_of_completion) / 100)


# System Settings

customtkinter.set_appearance_mode("System")
customtkinter.set_default_color_theme("blue")

# App frame
app = customtkinter.CTk()
app.geometry("720x480")
app.title("Youtube Downloader")

# Adding UI
title = customtkinter.CTkLabel(app, text="Youtube Link")
title.pack(padx=10, pady=10)

# Link Input
url_var = tkinter.StringVar()
link = customtkinter.CTkEntry(app, width=350, height=40, textvariable=url_var)
link.pack()

# Finished Downloading
finishLabel = customtkinter.CTkLabel(app, text="")
finishLabel.pack()

# Progress
pPercentage = customtkinter.CTkLabel(app, text="0%")
pPercentage.pack()

progressBar = customtkinter.CTkProgressBar(app, width=400)
progressBar.set(0)
progressBar.pack(padx=10, pady=10)

# Download button
download = customtkinter.CTkButton(app, text="Download", command=startDownload)
download.pack(padx=10, pady=10)

# Run App
app.mainloop()
