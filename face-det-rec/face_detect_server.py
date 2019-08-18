"""
Detect and recognize faces using dlib served by zerorpc

Should be called from a zerorpc client with ZoneMinder
alarm image metadata from zm-s3-upload.js.

This program should be run in the 'cv' virtual python environment, i.e.,
$ /home/lindo/.virtualenvs/cv/bin/python ./face_detect_server.py

This is part of the smart-zoneminder project.
See https://github.com/goruck/smart-zoneminder

Copyright (c) 2018, 2019 Lindo St. Angel
"""

import numpy as np
import cv2
import face_recognition
import json
import zerorpc
import logging
import pickle
import gevent
import signal

logging.basicConfig(level=logging.DEBUG)

# Get configuration.
with open('./config.json') as fp:
    config = json.load(fp)['faceDetServer']

# Heartbeat interval for zerorpc client in ms.
# This must match the zerorpc client config. 
ZRPC_HEARTBEAT = config['zerorpcHeartBeat']

# IPC (or TCP) socket for zerorpc.
# This must match the zerorpc client config.
ZRPC_PIPE = config['zerorpcPipe']

# Settings for face classifier.
# The model and label encoder need to be generated by 'train.py' first. 
MODEL_PATH = config['modelPath']
LABEL_PATH = config['labelPath']
MIN_PROBA = config['minProba']

# Images with Variance of Laplacian less than this are declared blurry. 
FOCUS_MEASURE_THRESHOLD = config['focusMeasureThreshold']

# Faces with width or height less than this are too small for recognition.
# In pixels.
MIN_FACE = config['minFace']

# Factor to scale image when looking for faces.
# May increase the probability of finding a face in the image. 
# Use caution setting the value > 1 since you may run out of memory.
# See https://github.com/ageitgey/face_recognition/wiki/Face-Recognition-Accuracy-Problems.
NUMBER_OF_TIMES_TO_UPSAMPLE = config['numFaceImgUpsample']

# Face detection model to use. Can be either 'cnn' or 'hog'.
FACE_DET_MODEL = config['faceDetModel']

# How many times to re-sample when calculating face encoding.
NUM_JITTERS = config['numJitters']

# Load face recognition model along with the label encoder.
with open(MODEL_PATH, 'rb') as fp:
	recognizer = pickle.load(fp)
with open(LABEL_PATH, 'rb') as fp:
	le = pickle.load(fp)

def face_classifier(encoding, min_proba):
	# perform classification to recognize the face based on 128D encoding
	# note: reshape(1,-1) converts 1D array into 2D
	preds = recognizer.predict_proba(encoding.reshape(1, -1))[0]
	j = np.argmax(preds)
	proba = preds[j]
	logging.debug('face classifier proba {} name {}'.format(proba, le.classes_[j]))
	if proba >= min_proba:
		name = le.classes_[j]
		logging.debug('face classifier says this is {}'.format(name))
	else:
		name = None # prob too low to recog face
		logging.debug('face classifier cannot recognize face')
	return name, proba

def variance_of_laplacian(image):
	# compute the Laplacian of the image and then return the focus
	# measure, which is simply the variance of the Laplacian
	return cv2.Laplacian(image, cv2.CV_64F).var()

def image_resize(image, width=None, height=None, inter=cv2.INTER_AREA):
    # ref: https://stackoverflow.com/questions/44650888/resize-an-image-without-distortion-opencv

    # initialize the dimensions of the image to be resized and
    # grab the image size
    dim = None
    (h, w) = image.shape[:2]

    # if both the width and height are None, then return the
    # original image
    if width is None and height is None:
        return image

    # check to see if the width is None
    if width is None:
        # calculate the ratio of the height and construct the
        # dimensions
        r = height / float(h)
        dim = (int(w * r), height)

    # otherwise, the height is None
    else:
        # calculate the ratio of the width and construct the
        # dimensions
        r = width / float(w)
        dim = (width, int(h * r))

    # resize the image
    resized = cv2.resize(image, dim, interpolation=inter)

    # return the resized image
    return resized

# Define zerorpc class.
class DetectRPC(object):
    def detect_faces(self, test_image_paths):
        # List that will hold all images with any face detection information. 
        objects_detected_faces = []

        # Loop over the images paths provided. 
        for obj in test_image_paths:
            logging.debug('**********Find Face(s) for {}'.format(obj['image']))
            for label in obj['labels']:
                # If the object detected is a person then try to identify face. 
                if label['name'] == 'person':
                    # Read image from disk. 
                    img = cv2.imread(obj['image'])
                    if img is None:
                        # Bad image was read.
                        logging.error('Bad image was read.')
                        label['face'] = None
                        continue

                    # First bound the roi using the coord info passed in.
                    # The roi is area around person(s) detected in image.
                    # (x1, y1) are the top left roi coordinates.
                    # (x2, y2) are the bottom right roi coordinates.
                    y2 = int(label['box']['ymin'])
                    x1 = int(label['box']['xmin'])
                    y1 = int(label['box']['ymax'])
                    x2 = int(label['box']['xmax'])
                    roi = img[y2:y1, x1:x2]
                    #cv2.imwrite('./roi.jpg', roi)
                    if roi.size == 0:
                        # Bad object roi...move on to next image.
                        logging.error('Bad object roi.')
                        label['face'] = None
                        continue

                    # Detect the (x, y)-coordinates of the bounding boxes corresponding
                    # to each face in the input image.
                    rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
                    #cv2.imwrite('./rgb.jpg', rgb)
                    detection = face_recognition.face_locations(rgb, NUMBER_OF_TIMES_TO_UPSAMPLE,
                        FACE_DET_MODEL)
                    if not detection:
                        # No face detected...move on to next image.
                        logging.debug('No face detected.')
                        label['face'] = None
                        continue

                    # Carve out face roi and check to see if large enough for recognition.
                    face_top, face_right, face_bottom, face_left = detection[0]
                    #cv2.rectangle(rgb, (face_left, face_top), (face_right, face_bottom), (255,0,0), 2)
                    #cv2.imwrite('./face_rgb.jpg', rgb)
                    face_roi = roi[face_top:face_bottom, face_left:face_right]
                    #cv2.imwrite('./face_roi.jpg', face_roi)
                    (f_h, f_w) = face_roi.shape[:2]
                    # If face width or height are not sufficiently large then skip.
                    if f_h < MIN_FACE or f_w < MIN_FACE:
                        logging.debug('Face too small to recognize.')
                        label['face'] = None
                        continue

                    # Compute the focus measure of the face
                    # using the Variance of Laplacian method.
                    # See https://www.pyimagesearch.com/2015/09/07/blur-detection-with-opencv/
                    gray = cv2.cvtColor(face_roi, cv2.COLOR_BGR2GRAY)
                    fm = variance_of_laplacian(gray)
                    # If fm below a threshold then face probably isn't clear enough
                    # for face recognition to work, so skip it. 
                    if fm < FOCUS_MEASURE_THRESHOLD:
                        logging.debug('Face too blurry to recognize.')
                        label['face'] = None
                        continue

                    # Find the 128-dimension face encoding for face in image.
                    # face_locations in css order (top, right, bottom, left)
                    face_location = (face_top, face_right, face_bottom, face_left)
                    encoding = face_recognition.face_encodings(rgb,
                        known_face_locations=[face_location], num_jitters=NUM_JITTERS)[0]
                    logging.debug('face encoding {}'.format(encoding))
                    # Perform classification on the encodings to recognize the face.
                    (name, proba) = face_classifier(encoding, MIN_PROBA)

                    # Add face name to label metadata.
                    label['face'] = name
                    # Add face confidence to label metadata.
                    # (First convert NumPy value to native Python type for json serialization.)
                    label['faceProba'] = proba.item()

	        # Add processed image to output list. 
            objects_detected_faces.append(obj)

        # Convert json to string and return data. 
        return(json.dumps(objects_detected_faces))

s = zerorpc.Server(DetectRPC(), heartbeat=ZRPC_HEARTBEAT)
s.bind(ZRPC_PIPE)
# Register graceful ways to stop server. 
gevent.signal(signal.SIGINT, s.stop) # Ctrl-C
gevent.signal(signal.SIGTERM, s.stop) # termination
# Start server.
# This will block until a gevent signal is caught
s.run()