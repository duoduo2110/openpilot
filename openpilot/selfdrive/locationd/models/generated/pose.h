#pragma once
#include "rednose/helpers/ekf.h"
extern "C" {
void pose_update_4(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void pose_update_10(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void pose_update_13(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void pose_update_14(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea);
void pose_err_fun(double *nom_x, double *delta_x, double *out_4922672802559952531);
void pose_inv_err_fun(double *nom_x, double *true_x, double *out_4675286663206909820);
void pose_H_mod_fun(double *state, double *out_30455509765476285);
void pose_f_fun(double *state, double dt, double *out_8436412409497388657);
void pose_F_fun(double *state, double dt, double *out_7299824336461252759);
void pose_h_4(double *state, double *unused, double *out_6774107777547476022);
void pose_H_4(double *state, double *unused, double *out_1025057040572079440);
void pose_h_10(double *state, double *unused, double *out_5889283512516384374);
void pose_H_10(double *state, double *unused, double *out_1898548047416842289);
void pose_h_13(double *state, double *unused, double *out_2892049699464882933);
void pose_H_13(double *state, double *unused, double *out_2187216784760253361);
void pose_h_14(double *state, double *unused, double *out_1913422395156722151);
void pose_H_14(double *state, double *unused, double *out_4107845472867451736);
void pose_predict(double *in_x, double *in_P, double *in_Q, double dt);
}