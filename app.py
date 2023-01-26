import pymkv
import os
import pytesseract
from pgsreader import PGSReader
from imagemaker import make_image
from tqdm import tqdm
from pysrt import SubRipFile, SubRipItem, SubRipTime
import srtchecker
import shutil
import threading
import sys
import time

edit = None # if the user wants to edit the subtitles before muxing
save_images = None # if the user wants to save the images extracted from the PGS subtitles
diff_langs = {} # if the user wants to use a different language for some subtitles
mkv = None

def diff_langs_from_text(text) -> dict[str, str]:
    if text == "":
        return {}
    
    lines = text.splitlines()
    diff_langs = {}
    for line in lines:
        if "->" in line:
            old_lang, new_lang = line.split("->")

            old_lang = old_lang.strip()
            new_lang = new_lang.strip()

            diff_langs[old_lang] = new_lang
        elif line.strip() != "":
            print(f"Invalid input: {line}")

    return diff_langs

# helper function for threading
def extract(file_path: str, track_id: int):
    os.system(f"mkvextract \"{file_path}\" tracks {track_id}:{track_id}.sup")

def extract_subtitles(file_path: str) -> list[int]: 
    # TODO get path from mkv instead of parameter
    subtitle_ids = []
    thread_pool = []

    for track in mkv.tracks:
        if track.track_type == "subtitles":

            track: pymkv.MKVTrack
            track_id = track.track_id

            if track.track_codec != "HDMV PGS":
                continue

            thread = threading.Thread(name=f"Extract subtitle #{track_id}", target=extract, args=(file_path, track_id))
            thread.start()
            thread_pool.append(thread)

            subtitle_ids.append(track_id)

    for thread in thread_pool:
        thread.join()
            
    return subtitle_ids

def get_lang(lang_code: str) -> str | None:

    new_lang = diff_langs.get(lang_code) # check if user wants to use a different language

    if new_lang is  not None:
        if new_lang in pytesseract.get_languages():
            return new_lang
        else:
            print(f"Language {new_lang} is not installed, using {lang_code} instead")

    if lang_code in pytesseract.get_languages(): # when user doesn't want to change language or changed language is not installed
        return lang_code
    else:
        return None

def convert_to_srt(lang:str, track_id: int, img_dir:str='', save_images:bool=False):
    srt_file = f"{track_id}.srt"
    pgs_file = f"{track_id}.sup"
    pgs = PGSReader(pgs_file)
    srt = SubRipFile()
    
    if save_images:
        os.makedirs(img_dir, exist_ok=True)

    # loading DisplaySets
    all_sets = [ds for ds in tqdm(pgs.iter_displaysets(), unit="ds")]

    # building SRT file from DisplaySets
    sub_text = ""
    sub_start = 0
    sub_index = 0
    for ds in tqdm(all_sets, unit="ds"):
        if ds.has_image:
            pds = ds.pds[0] # get Palette Display Segment
            ods = ds.ods[0] # get Object Display Segment
            img = make_image(ods, pds)
            
            if save_images:
                img.save(f"{img_dir}/{sub_index}.jpg")
            
            sub_text = pytesseract.image_to_string(img, lang)
            sub_start = ods.presentation_timestamp
        else:
            start_time = SubRipTime(milliseconds=int(sub_start))
            end_time = SubRipTime(milliseconds=int(ds.end[0].presentation_timestamp))
            srt.append(SubRipItem(sub_index, start_time, end_time, sub_text))
            sub_index += 1

    # check and save SRT file
    srt.save(srt_file)
    srtchecker.check_srt(srt_file, True)

def replace_subtitles(subtitle_ids: list[int], file_name: str):
    deleted_tracks = 0

    print(f"Replacing subtitles in {file_name}...")
    for track_id in subtitle_ids:
        # if a subtitle was deleted during editing
        if not os.path.exists(f"{track_id}.srt"):
            mkv.remove_track(track_id - deleted_tracks)
            deleted_tracks += 1
            continue

        track = mkv.tracks[track_id - deleted_tracks]
        # make new track from new .srt file and settings from old PGS subtitle
        new_sub = pymkv.MKVTrack(f"{track_id}.srt", track_name=track.track_name, language=track.language, default_track=track.default_track, forced_track=track.forced_track)
        mkv.replace_track(track_id - deleted_tracks, new_sub)

# estimate new file size based on size of new subtitles
def calc_size(old_size: int, subtitle_ids: list[int]) -> int:
    new_size = old_size
    for track_id in subtitle_ids:
        new_size -= os.path.getsize(f"{track_id}.sup")
        if os.path.exists(f"{track_id}.srt"):
            new_size += os.path.getsize(f"{track_id}.srt")
    return new_size

def mux_file(subtitle_ids: list[int], file_path: str):
    print("Muxing file...")
    file_size = os.path.getsize(file_path)
    file_name = os.path.splitext(os.path.basename(file_path))[0]
    new_file_dir = os.path.dirname(file_path)
    new_file_name = f"{new_file_dir}/{file_name} (1).mkv"
    old_file_size = 0

    pbar = tqdm(total=calc_size(file_size, subtitle_ids), unit='B', unit_scale=True, unit_divisor=1024)

    thread = threading.Thread(name="Muxing", target=mkv.mux, args=(new_file_name, True))
    thread.start()

    while thread.is_alive():
        new_file_size = os.path.getsize(f"{file_name} (1).mkv")
        pbar.update(new_file_size - old_file_size)
        old_file_size = new_file_size
        time.sleep(0.1)

    pbar.close()

# remove file that may not exist anymore without throwing an error
def silent_remove(file: str):
    try:
        os.remove(file)
    except OSError:
        pass

def clean(subtitle_ids):
    print("Cleaning up...")
    if save_images:
        shutil.rmtree("img/")
    for track_id in subtitle_ids:
        silent_remove(f"{track_id}.sup")
        silent_remove(f"{track_id}.srt")

def main(file_paths: list[str], edit2: bool, save_images2: bool, diff_langs2: dir):
    global edit, save_images, diff_langs, mkv
    edit = edit2
    save_images = save_images2
    diff_langs = diff_langs2

    for file_path in file_paths:
        try:

            file_name = os.path.splitext(os.path.basename(file_path))[0]
            mkv = pymkv.MKVFile(file_path)
            thread_pool = []

            print(f"Processing {file_name}...")
            subtitle_ids = extract_subtitles(file_path)

            # skip title if no PGS subtitles were found
            if len(subtitle_ids) == 0:
                print("No subtitles found.\n")
                continue

            # convert PGS subtitles to SRT subtitles
            for id in subtitle_ids:
                track = mkv.tracks[id]

                # get language used in subtitle
                lang_code = track.language
                language = get_lang(lang_code)

                thread = threading.Thread(name=f"Convert subtitle #{id}", target=convert_to_srt, args=(language, id, f"img/{file_name}/{id}/", save_images))
                thread.start()
                thread_pool.append(thread)

            for thread in thread_pool:
                thread.join()

            if edit:
                print("You can now edit the SRT files. Press Enter when you are done.")
                input()

            replace_subtitles(subtitle_ids, file_name)

            # create empty .mkv file
            new_file_dir = os.path.dirname(file_path)
            new_file_name = f"{new_file_dir}/{file_name} (1).mkv"

            open(new_file_name, "w").close()
            
            mux_file(subtitle_ids, file_path)  
            clean(subtitle_ids)

            print(f"Finished {file_name}\n")
        except Exception as e:
            print(f"Error while processing {file_name}: {e}\n")

if __name__ == "__main__":
    try:
        edit = sys.argv[1] == '1'
        save_images = sys.argv[2] == '1'
    except IndexError:
        edit = False if edit is None else edit
        save_images = False if save_images is None else save_images

    print("Do you want to use a different language for some subtitles?")
    print("1) Yes")
    print("2) No (Default)")

    answer = input("Your Input: ")
    if answer == '1':
        while answer.strip() != "":
            print("Enter your changes like this: ger->eng : " )
            answer = input("Your Input: ")

            if "->" in answer:
                old_lang, new_lang = answer.split("->")
                old_lang = old_lang.strip()
                new_lang = new_lang.strip()

                diff_langs[old_lang] = new_lang
                print("Added language change.")
            elif answer.strip() != "":
                print("Invalid input. Try again.")
        
        print("Starting conversion...\n")

    main(os.listdir(), edit, save_images, diff_langs)
