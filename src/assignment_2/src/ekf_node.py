#! /usr/bin/env python
import signal
import math
from nav_msgs.msg import Odometry
import rospy
import numpy as np
import tf
from sensor_msgs.msg import NavSatFix
import geonav_conversions as gc
import pandas as pd


class FuseDataEKF:
    def __init__(self):
        # Initialise Node
        rospy.init_node('EKF')
        # Subscribe to Visual Odometry and GPS measurements
        self.odomsub = rospy.Subscriber(
            '/visual_odometry', Odometry, self.get_odometry, queue_size=1)
        self.gpssub = rospy.Subscriber(
            '/gps', NavSatFix, self.get_gps, queue_size=1)
        # Initialise relevant class members
        self.visOdometry = Odometry()			# Visual Odometry
        # Fill in frame id to make it related to odom
        self.visOdometry.header.frame_id = 'odom'
        self.gpsdata = Odometry()				# GPS data
        self.gpsdata.header.frame_id = 'odom'
        self.ekfdata = Odometry()				# Output EKF data
        self.ekfdata.header.frame_id = 'odom'
        # Publish all data in correct format for each data source after we have extracted relevant fields in each.
        self.odompub = rospy.Publisher('/odometry_exp', Odometry, queue_size=1)
        self.gpspub = rospy.Publisher('/gps_exp', Odometry, queue_size=1)
        self.ekfpub = rospy.Publisher('/fused_pose_yehia', Odometry, queue_size=1)

        self.gps = True		# Flag to initialise first GPS data
        self.vo = False		# Flag to tell if a new vo data point has arrived
        self.posx = 0		# Initial position and orientation of robot.
        self.posy = 0
        self.yaw = 0
        # initial position estimate
        self.Mt = np.array([[0],
                            [0],
                            [0]])
        # initial covariance estimate
        self.sigma_t = np.array([[1, 0, 0],
                                [0, 1, 0],
                                [0, 0, (math.pi/180)**2]])
        # Arrays to store data and export to csv
        self.fusedMatrix = []  # this will store fused data for report
        self.voMatrix = []  # this will store Vo data for report
        self.gpsMatrix = []  # this will store gps data for report

        # odom noise NEEDS TO BE TUNED
        self.Rt = np.array([[1, 0, 0],
                            [0, 1, 0],
                            [0, 0, 1]]) * 2**5
                            #          0.01**2
        # gps noise NEEDS TO BE TUNED - OR USE THE LIVE DATA
        self.Qt = np.array([[1.0, 0.0],
                            [0.0, 1.0]]) * 10**2
                            #             100**2    

    def get_odometry(self, msg):

        # linear velocity
        self.XvelLin = msg.twist.twist.linear.x
        self.YvelLin = msg.twist.twist.linear.y
        self.ZvelLin = msg.twist.twist.linear.z
        self.Vel = math.hypot(self.XvelLin, self.YvelLin)
        # angular Velocity
        self.XvelAng = msg.twist.twist.angular.x
        self.YvelAng = msg.twist.twist.angular.y
        self.ZvelAng = - msg.twist.twist.angular.z  # dtheta

        self.visOdometry.header.stamp = rospy.Time.now()

        # new position values based on velocity and motion model ( x(k) = x(k-1)+Vx*cos(theta); y(k)= y(k-1)*Vx * sin (theta)
        self.posx = self.posx + \
            np.cos(self.yaw) * self.XvelLin - np.sin(self.yaw) * self.YvelLin
        self.posy = self.posy + \
            np.sin(self.yaw) * self.XvelLin + np.cos(self.yaw) * self.YvelLin
        self.yaw = self.yaw + self.ZvelAng
        # print('posx', self.posx, 'posy', self.posy, 'angle', self.yaw)
        self.visOdometry.pose.pose.position.x = self.posx
        self.visOdometry.pose.pose.position.y = self.posy
        self.visOdometry.pose.pose.orientation.z = self.yaw
        self.voMatrix.append(
            [rospy.Time.now(), self.posx, self.posy, self.yaw])
        # covariance len 36 (6 for position and 6 for velocities), 36 in all for each covariance
        self.visOdometry.pose.covariance = [
            1 for k in range(len(msg.pose.covariance))]
        self.visOdometry.twist.covariance = [
            1 for k in range(len(msg.twist.covariance))]

        # Note that we need to use quaternions to visualise publish message in rviz.
        # You will have to do the same in your code.
        quat = tf.transformations.quaternion_from_euler(0.0, 0.0, self.yaw)
        self.visOdometry.pose.pose.orientation.z = quat[2]
        self.visOdometry.pose.pose.orientation.w = quat[3]
        self.odompub.publish(self.visOdometry)

        # extended kalman filter prediction step
        self.ekf_prediction()
        self.vo = True

    def get_gps(self, msg):
        if self.gps is True:
            self.lat_origin = msg.latitude
            self.longi_origin = msg.longitude
            self.gps = False
        lat = msg.latitude
        longi = msg.longitude
        # print(self.origin)
        # Convert Lat long to UTM (x,y positions)
        self.Xgps, self.Ygps = gc.ll2xy(
            lat, longi, self.lat_origin, self.longi_origin)
        self.gpsdata.pose.pose.position.x = self.Xgps
        self.gpsdata.pose.pose.position.y = self.Ygps
        # Save gps CSV file position
        self.gpsMatrix.append(
            [rospy.Time.now(), self.gpsdata.pose.pose.position.x, self.gpsdata.pose.pose.position.y, 0])
        # Publish Data
        self.gpspub.publish(self.gpsdata)
        # Use this to update GPS covariance on the fly
        # self.Rt[0][0] = msg.position_covariance[0]
        # self.Rt[1][1] = msg.position_covariance[4]
        # print('RT', self.Rt)
        # Now do update for EKF
        if self.vo is True:  # We have new prediction, do update
            self.ekf_update()
            self.vo = False  # We wait for the next vo to do an update

    # remember to udpate Mt and Sigma_t after each prediction
    # as this is an unsynchronuous process with the gps ands you
    # need to keep updating position and covariances
    def ekf_prediction(self):
        # PUT YOUR CODE HERE - FOLLOW EQUATIONS IN NOTES
        #Some Code Adapted from github 
        #amolloma/Sensor_Fusion_Exercises and
        #Sina-Baharlou/Pose-Estimation-EKF

        # Inintial Pose Estimate (Mu)
        # Equation No. 2 EKF Algorithm
        self.Mth = np.array([[np.cos(self.yaw) * self.XvelLin - np.sin(self.yaw) * self.YvelLin],
                              [np.sin(self.yaw) * self.XvelLin + np.cos(self.yaw) * self.YvelLin],
                              [self.ZvelAng]])
        self.Mt = np.add(self.Mt , self.Mth)
    
        # Jacobian Motion Model
        A = ((-self.XvelLin*np.sin(self.Mt[2][0])) - (self.YvelLin*np.cos(self.Mt[2][0])))
        
        B = ((self.XvelLin*np.cos(self.Mt[2][0])) - (self.YvelLin*np.sin(self.Mt[2][0])))     

        Gt = np.array([[1, 0 , A],
                        [0, 1, B],
                        [0, 0, 1]])

        # Covariance (Sigma)
        # Equation No. 3 EKF Algorithm
        sig = np.dot(Gt, self.sigma_t)
        sig = np.dot(sig, Gt.T)
        self.sigma_t = np.add(sig, self.Rt)
        
        # Saved positions. Note that we need to use quaternions to publish in rviz
        quat = tf.transformations.quaternion_from_euler(
            0.0, 0.0, self.Mt[2][0])
        # Get time for ekf frame
        self.ekfdata.header.stamp = rospy.Time.now()
        self.ekfdata.pose.pose.position.x = self.Mt[0][0]
        self.ekfdata.pose.pose.position.y = self.Mt[1][0]
        self.ekfdata.pose.pose.orientation.z = quat[2]
        self.ekfdata.pose.pose.orientation.w = quat[3]
        # update position estimate

        # print('Mt', self.Mt)
        self.fusedMatrix.append(
            [rospy.Time.now(), self.Mt[0][0], self.Mt[1][0], self.Mt[2][0]])
        self.ekfpub.publish(self.ekfdata)

    def ekf_update(self):
        # when new gps data comes in
        # ADD YOUR CODE HERE

        # Jacobian Obesrvation Model
        Ht = np.array([[1, 0, 0],
                        [0, 1, 0]])

        #self.z =np.array([self.Xgps], [self.Ygps])
        self.z = np.array([[self.gpsdata.pose.pose.position.x],
                           [self.gpsdata.pose.pose.position.y]])       
        I = np.identity(3)

        # Equation No. 4 EKF Algorithm - Kt = Sigma_t * Ht.T (Ht*Sigma_t*Ht.T+Qt)**-1

        #Kt = np.dot(self.sigma_t, Ht.T)
        #Kt = np.dot(Kt, Ht.T)
       # #Kt = (Kt @ Ht.T)
       # #Kt = np.matmul(Kt, Ht.T)
       # Kt = Kt * Ht.T
       # Kt = 1 / np.add(Kt, self.Qt)
        #Kt = np.add(Kt, self.Qt)
        #Kt = np.linalg.inv(Kt)
       # K2 = np.dot(self.sigma_t, Ht.T)
       # Kt = np.dot( K2, Kt)

        Kt = np.dot(Ht, self.sigma_t)
        Kt = np.dot(Kt, Ht.T)
        Kt = np.add(Kt, self.Qt)
        Kt = np.linalg.inv(Kt)
        Kt_1 = np.dot(self.sigma_t, Ht.T)
        Kt = np.dot(Kt_1, Kt)

        # Equation no.5 EKF Algorithm - Mt = Mt_h + Kt(zt- h(Mt_h))
        hmt= np.dot(Ht, self.Mt)
        zhmt = np.subtract( self.z, hmt)
        zhmt = np.dot( Kt, zhmt)
        self.Mt = np.add(self.Mt, zhmt)
     
        # Equation No.6 EKF Algorithm
        kh = np.dot(Kt, Ht)
        kh = np.subtract(I, kh)
        self.sigma_t = np.dot(kh, self.sigma_t)
        print("update done")


    def csv(self):
        df_fused = pd.DataFrame(self.fusedMatrix, columns=[
            'timestamp', 'x', 'y', 'yaw'])
        df_fused.to_csv('/home/user/catkin_ws/src/assignment_2/csv/fused_estimates_exp.csv',
                        index=False, header=True)
        df_gps = pd.DataFrame(self.gpsMatrix, columns=[
            'timestamp', 'x', 'y', 'yaw'])
        df_gps.to_csv('/home/user/catkin_ws/src/assignment_2/csv/gps_estimates_exp.csv',
                      index=False, header=True)
        df_vo = pd.DataFrame(self.voMatrix, columns=[
            'timestamp', 'x', 'y', 'yaw'])
        df_vo.to_csv('/home/user/catkin_ws/src/assignment_2/csv/vo_estimates_exp.csv',
                     index=False, header=True)
        print('saved Data')


def ExitAndSaveResults(signal, frame):
    print("Program Complete Or Interrupted, Saving results to date")
    mot.csv()


# activating the motion class
if __name__ == "__main__":
    signal.signal(signal.SIGINT, ExitAndSaveResults)
    mot = FuseDataEKF()
    rospy.spin()
