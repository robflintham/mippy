#~ from multiprocessing import Pool
from multiprocessing.dummy import Pool as ThreadPool
import os
import dicom
from . import io
from . import pixel
from pkg_resources import resource_filename
from subprocess import call
import cPickle as pickle
import numpy as np
import sys
from PIL import Image
import gc

from ..viewing import get_overlay
from ..threading import multithread
from functools import partial
from mrenhanced import get_frame_ds
import itertools

def recursive_file_finder(path):
	pathlist = []
	for root,directories,files in os.walk(path):
		for filename in files:
			filepath = os.path.join(root,filename)
			pathlist.append(filepath)
	return pathlist

def get_instance_uid(dicomfile):
	#~ try:
	ds = dicom.read_file(dicomfile)
	io.save_temp_ds(ds,r'K:\MIPPYTEMP',ds.SOPInstanceUID)
	#~ except:
		#~ return None
	return ds.SOPInstanceUID

def multidicomread(paths,threads=None):
	if threads:
		pool = ThreadPool(threads)
	else:
		pool = ThreadPool()
	results = pool.map(get_instance_uid, paths)
	pool.close()
	pool.join()
	return results

def get_dataset(info,tempdir=None):
	"""
	This function uses instance UID and/or filepath and/or instance
	number to find a single DICOM dataset. It checks existing MIPPY
	temp files, and if it can't be found, loads the file/instance asked
	for. This allows parallelisation of the process to speed up loading
	of datasets.
	info is a tuple of:
	info[0] = UID (from MIPPY, i.e. including instance number for
			enhanced DICOM)
	info[1] = absolute file path
	info[2] = instance number
	
	UID cannot be None. The other two can, but it might not find anything.
	"""
	uid = info[0]
	filepath = info[1]
	instance = info[2]
	
	# First, check for UID in temp files
	if not uid is None:
		temppath = os.path.join(tempdir,str(uid)+'.mds')
		if os.path.exists(temppath):
			print "TEMP FILE FOUND: "+str(uid)
			with open(temppath,'rb') as tempfile:
				ds = pickle.load(tempfile)
			return ds
		elif not filepath is None:
			ds = dicom.read_file(filepath)
			if not 'ENHANCED' in str(ds.SOPClassUID).upper():
				print "SINGLE DICOM FILE FOUND"
				io.save_temp_ds(ds,tempdir,str(ds.SOPInstanceUID)+'.mds')
				return ds
			else:
				if not instance is None:
					from mrenhanced import get_frame_ds
					ds_split = get_frame_ds(instance,ds)
					io.save_temp_ds(ds_split,tempdir,str(ds.SOPInstanceUID)+"_"+str(i).zfill(3)+'.mds')
					return ds_split
	
	# If it's got this far, cannot find file or not enough info
	print "CANNOT FIND FILE"
	return None


def collect_dicomdir_info(path,tempdir=None,force_read=False):
	tags=[]
	ima_mrs_uid = '1.3.12.2.1107.5.9.1'
	# Variables to contain path to dcmdjpeg for compressed DICOM
	dcmdjpeg = None
	# This automatically excludes Philips "XX_" files, but only based on name.  If they've been renamed they
	# won't be picked up until the "try/except" below.
	if os.path.split(path)[1].startswith("XX_"):
		return tags
#	print os.path.split(path)[1]
	
	# Remove any previous datasets just held as "ds" in memory
	ds = None
	#Read file, if not DICOM then ignore
	try:
		ds = dicom.read_file(path, force=force_read)
		# Removed this garbage collection call to try speed up directory reading
		#~ gc.collect()
	except Exception:
		print path+'\n...is not a valid DICOM file and is being ignored.'
		return tags
	if ds:
		#~ print path
			
		try:
			# There has to be a better way of testing this...?
			# If "ImageType" tag doesn't exist, then it's probably an annoying "XX" type file from Philips
			type = ds.ImageType
		except Exception:
			return tags
		# Ignore "OT" (other?) modality DICOM objects - for now at least...
		# Particularly for Siemens 3D data viewing!
		modality = ds.Modality
		if (
			'OT' in modality
			):
			return tags
		
		transfer_syntax =  str(ds.file_meta[0x2,0x10].value)
		if 'JPEG' in transfer_syntax:
			compressed = True
		else:
			compressed = False
		try:
			# Some manufacturers use a handy "series description" tag, some don't
			seriesdesc = ds.SeriesDescription
		except Exception:
			try:
				# Some store "protocol name", which will do for now until I find something better
				seriesdesc = ds.ProtocolName
			except Exception:
				# If all else fails, just use a generic string
				seriesdesc = "Unknown Study Type"
		
		if "PHOENIXZIPREPORT" in seriesdesc.upper():
			# Ignore any phoenix zip report files from Siemens
			return tags
		# Unless told otherwise, assume normal MR image storage
		mode = "Assumed MR Image Storage"
		try:
			mode = str(ds.SOPClassUID)
		except Exception:
			pass
		if "SOFTCOPY" in mode.upper() or "BASIC TEXT" in mode.upper():
			# Can't remember why I have these, I think they're possible GE type files???
			return tags
		if mode.upper()=="ENHANCED MR IMAGE STORAGE":
			# If enhanced file, record number of frames.  This is important for pulling the right imaging
			# data out for the DICOM tree and image previews
			enhanced = True
			frames = ds.NumberOfFrames
		else:
			enhanced = False
			frames = 1
		study_uid = ds.StudyInstanceUID
		series_uid = ds.SeriesInstanceUID
		name = ds.PatientName
		date = ds.StudyDate
		series = ds.SeriesNumber
		time = ds.StudyTime
		try:
			# Some manufacturers use a handy "study description" tag, some don't
			studydesc = ds.StudyDescription
		except Exception:
			try:
				# Philips stores "body part examined", which will do for now until I find something better
				studydesc = ds.BodyPartExamined
			except Exception:
				# If all else fails, just use a generic string
				studydesc = "Unknown Study Type"
		
		#~ if tags is None:
			#~ tags = []
		
		if enhanced:
			# Set "instance" array to match number of frames
			instance = np.array(range(frames))+1
		else:
			# Or if not enhanced/multi-frame, just create a single element list so that the code
			# below still works
			try:
				instance = [ds.InstanceNumber]
			except AttributeError:
				print "INSTANCE NUMBER TAG DOESN'T EXIST"
				print path
				raise
		
		instance_uid = 'UNKNOWN'
		
		for i in instance:
			if not enhanced:
				instance_uid = ds.SOPInstanceUID
			else:
				# Append instance UID with the frame number to give unique reference to each slice
				instance_uid = ds.SOPInstanceUID+"_"+str(i).zfill(4)
		
			if compressed:
				# Check if temp file already exists for that InstanceUID. If so, read that file. If not, 
				# uncompress the file and replace ds. Dataset will get saved as temp file at the end of this
				# function.
				temppath = os.path.join(tempdir,instance_uid+'.mds')
				if os.path.exists(temppath):
					print seriesdesc+' '+str(i).zfill(3)
					print "    COMPRESSED DICOM - Temp file found"
					with open(temppath,'rb') as tempfile:
						ds = pickle.load(tempfile)
					tempfile.close()
				else:
					# Set path to dcmdjpeg if necessary
					if dcmdjpeg is None:
						if 'darwin' in sys.platform:
							#~ dcmdjpeg= resource_filename('mippy','resources/dcmdjpeg_mac')
							dcmdjpeg = os.path.join(tempdir,"dcmdjpeg_mac")
						elif 'linux' in sys.platform:
							#~ dcmdjpeg=resource_filename('mippy','resources/dcmdjpeg_linux')
							dcmdjpeg = os.path.join(tempdir,"dcmdjpeg_linux")
						elif 'win' in sys.platform:
							#~ dcmdjpeg=resource_filename('mippy','resources\dcmdjpeg_win.exe')
							dcmdjpeg = os.path.join(tempdir,"dcmdjpeg_win.exe")
						else:
							print "UNSUPPORTED OPERATING SYSTEM"
							print str(sys.platform)
					# Uncompress the file
					outpath=os.path.join(tempdir,'UNCOMP_'+instance_uid+'.DCM')
					print seriesdesc+' '+str(i).zfill(3)
					print "    COMPRESSED DICOM - Uncompressing"
					#~ if 'darwin' in sys.platform:
						#~ #dcmdjpeg=os.path.join(os.getcwd(),'lib','dcmdjpeg_mac')
						#~ dcmdjpeg=r'./lib/dcmdjpeg_mac'
					#~ elif 'linux' in sys.platform:
						#~ dcmdjpeg=r'./lib/dcmdjpeg_linux'
					#~ elif 'win' in sys.platform:
						#~ dcmdjpeg=r'lib\dcmdjpeg_win.exe'
					#~ else:
						#~ print "UNSUPPORTED OPERATING SYSTEM"
						#~ print str(sys.platform)
					#~ command = [dcmdjpeg,'\"'+path+'\"','\"'+outpath+'\"']
					command = [dcmdjpeg,path,outpath]
					call(command, shell=False)
					#~ path = outpath
					ds = dicom.read_file(outpath)
				
			print name,"/",date,"/",seriesdesc,"/",i
			if not ("SPECTROSCOPY" in mode.upper() or ima_mrs_uid in mode.upper()):
				pxfloat=pixel.get_px_array(ds,enhanced,i,bitdepth=8)
				if pxfloat is None:
					continue
				try:
					overlay = get_overlay(ds)
					pxfloat[np.where(overlay>0)]=255
					#~ pxfloat += overlay
				except KeyError:
					pass
				except:
					raise
			else:
				mrs_image =  resource_filename('mippy','resources/mrs.png')
				pxfloat = np.asarray(Image.open(mrs_image)).astype(np.float64)
				pxfloat = np.mean(pxfloat,axis=2)
				series_uid = series_uid+'.RAW'
				seriesdesc = '<MRS> '+seriesdesc
			
			# Append the information to the "tag list" object
			tags.append(dict([('date',date),('time',time),('name',name),('studyuid',study_uid),
					('series',series),('seriesuid',series_uid),('studydesc',studydesc),
					('seriesdesc',seriesdesc),('instance',i),('instanceuid',instance_uid),
					('path',path),('enhanced',enhanced),('compressed',compressed),
					('px_array',pxfloat)]))
			#~ print tags[-1]['seriesdesc'],tags[-1]['instance']
		# Assuming all this has worked, serialise the dataset (ds) for later use, with the instance UID
		# as the file name
		if not enhanced:
			if tempdir:
				io.save_temp_ds(ds,tempdir,instance_uid+'.mds')
	
	del ds
		
	return tags

def compare_dicom(ds1,ds2,diffs=None,num=None,name=''):
	if diffs is None:
		diffs = []
		#~ gc.collect()
	if num:
		num=' ('+str(num).zfill(4)+')'
	else:
		num=''
	exclude_list = ['UID',
				'REFERENCE',	# Don't care what localisers were used
				'SERIES TIME',
				'ACQUISITION TIME',
				'CONTENT TIME',
				'CREATION TIME',
				'PIXEL VALUE',
				'WINDOW',
				'CSA',		# Still don't really know what CSA is
				'PIXEL DATA',
				'PADDING']
	for element in ds1:
		if any(s in element.name.upper() for s in exclude_list):
			continue
		val1 = element.value
		try:
			val2 = ds2[element.tag].value
		except:
			diffs.append((name+str(element.name)+num,str(val1),'--MISSING--'))
			continue
		if element.VR=="SQ":
			for i in range(len(val1)):
				compare_dicom(val1[i],val2[i],diffs=diffs,num=i,name=str(element.name)+' >> ')
			continue
		if not val1==val2:
			if not any(s in element.name.upper() for s in exclude_list):
				diffs.append((name+str(element.name)+num,str(val1),str(val2)))
	for element in ds2:
		val2 = element.value
		try:
			val1 = ds1[element.tag].value
		except:
			diffs.append((name+str(element.name)+num,'--MISSING--',str(val2)))
			continue
	
	return diffs

def load_images_from_uids(list_of_tags,uids_to_match,tempdir,multiprocess=False):
	datasets_to_pass = []
	dcm_info = []
	previous_tag = None
	open_file = None
	if not multiprocess or ('win' in sys.platform and not 'darwin' in sys.platform and len(uids_to_match)<25):
		for tag in list_of_tags:
			if tag['instanceuid'] in uids_to_match or tag['seriesuid'] in uids_to_match:
				# Check to see if new series
				if previous_tag:
					if tag['seriesuid']==previous_tag['seriesuid']:
						new_series = False
					else:
						new_series = True
				else:
					new_series = True
				# First, check if dataset is already in temp files
				temppath = os.path.join(tempdir,tag['instanceuid']+'.mds')
				if os.path.exists(temppath):
					#~ print "TEMP FILE FOUND",tag['instanceuid']
					with open(temppath,'rb') as tempfile:
						if new_series:
							datasets_to_pass.append([pickle.load(tempfile)])
						else:
							datasets_to_pass[-1].append(pickle.load(tempfile))
						tempfile.close()
				else:
					if not tag['path']==open_file:
						open_ds = dicom.read_file(tag['path'])
						open_file = tag['path']
						#~ gc.collect()
					if not tag['enhanced']:
						if new_series:
							datasets_to_pass.append([open_ds])
						else:
							datasets_to_pass[-1].append(open_ds)
					else:
						split_ds = get_frame_ds(tag['instance'],open_ds)
						if new_series:
							datasets_to_pass.append([split_ds])
						else:
							datasets_to_pass[-1].append(split_ds)
						io.save_temp_ds(split_ds,tempdir,tag['instanceuid']+'.mds')
				previous_tag = tag
	else:
		for tag in list_of_tags:
			if tag['instanceuid'] in uids_to_match:
				dcm_info.append((tag['instanceuid'],tag['path'],tag['instance']))
		gc.collect()
		f = partial(get_dataset,tempdir=tempdir)
		datasets_to_pass = multithread(f,dcm_info)
		# Group by series, to be flattened later if 1D list required
		datasets_to_pass = [list(g) for k,g, in itertools.groupby(datasets_to_pass, lambda ds: ds.SeriesInstanceUID)]
	
	return datasets_to_pass