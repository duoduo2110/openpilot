#include "pose.h"

namespace {
#define DIM 18
#define EDIM 18
#define MEDIM 18
typedef void (*Hfun)(double *, double *, double *);
const static double MAHA_THRESH_4 = 7.814727903251177;
const static double MAHA_THRESH_10 = 7.814727903251177;
const static double MAHA_THRESH_13 = 7.814727903251177;
const static double MAHA_THRESH_14 = 7.814727903251177;

/******************************************************************************
 *                      Code generated with SymPy 1.14.0                      *
 *                                                                            *
 *              See http://www.sympy.org/ for more information.               *
 *                                                                            *
 *                         This file is part of 'ekf'                         *
 ******************************************************************************/
void err_fun(double *nom_x, double *delta_x, double *out_4922672802559952531) {
   out_4922672802559952531[0] = delta_x[0] + nom_x[0];
   out_4922672802559952531[1] = delta_x[1] + nom_x[1];
   out_4922672802559952531[2] = delta_x[2] + nom_x[2];
   out_4922672802559952531[3] = delta_x[3] + nom_x[3];
   out_4922672802559952531[4] = delta_x[4] + nom_x[4];
   out_4922672802559952531[5] = delta_x[5] + nom_x[5];
   out_4922672802559952531[6] = delta_x[6] + nom_x[6];
   out_4922672802559952531[7] = delta_x[7] + nom_x[7];
   out_4922672802559952531[8] = delta_x[8] + nom_x[8];
   out_4922672802559952531[9] = delta_x[9] + nom_x[9];
   out_4922672802559952531[10] = delta_x[10] + nom_x[10];
   out_4922672802559952531[11] = delta_x[11] + nom_x[11];
   out_4922672802559952531[12] = delta_x[12] + nom_x[12];
   out_4922672802559952531[13] = delta_x[13] + nom_x[13];
   out_4922672802559952531[14] = delta_x[14] + nom_x[14];
   out_4922672802559952531[15] = delta_x[15] + nom_x[15];
   out_4922672802559952531[16] = delta_x[16] + nom_x[16];
   out_4922672802559952531[17] = delta_x[17] + nom_x[17];
}
void inv_err_fun(double *nom_x, double *true_x, double *out_4675286663206909820) {
   out_4675286663206909820[0] = -nom_x[0] + true_x[0];
   out_4675286663206909820[1] = -nom_x[1] + true_x[1];
   out_4675286663206909820[2] = -nom_x[2] + true_x[2];
   out_4675286663206909820[3] = -nom_x[3] + true_x[3];
   out_4675286663206909820[4] = -nom_x[4] + true_x[4];
   out_4675286663206909820[5] = -nom_x[5] + true_x[5];
   out_4675286663206909820[6] = -nom_x[6] + true_x[6];
   out_4675286663206909820[7] = -nom_x[7] + true_x[7];
   out_4675286663206909820[8] = -nom_x[8] + true_x[8];
   out_4675286663206909820[9] = -nom_x[9] + true_x[9];
   out_4675286663206909820[10] = -nom_x[10] + true_x[10];
   out_4675286663206909820[11] = -nom_x[11] + true_x[11];
   out_4675286663206909820[12] = -nom_x[12] + true_x[12];
   out_4675286663206909820[13] = -nom_x[13] + true_x[13];
   out_4675286663206909820[14] = -nom_x[14] + true_x[14];
   out_4675286663206909820[15] = -nom_x[15] + true_x[15];
   out_4675286663206909820[16] = -nom_x[16] + true_x[16];
   out_4675286663206909820[17] = -nom_x[17] + true_x[17];
}
void H_mod_fun(double *state, double *out_30455509765476285) {
   out_30455509765476285[0] = 1.0;
   out_30455509765476285[1] = 0.0;
   out_30455509765476285[2] = 0.0;
   out_30455509765476285[3] = 0.0;
   out_30455509765476285[4] = 0.0;
   out_30455509765476285[5] = 0.0;
   out_30455509765476285[6] = 0.0;
   out_30455509765476285[7] = 0.0;
   out_30455509765476285[8] = 0.0;
   out_30455509765476285[9] = 0.0;
   out_30455509765476285[10] = 0.0;
   out_30455509765476285[11] = 0.0;
   out_30455509765476285[12] = 0.0;
   out_30455509765476285[13] = 0.0;
   out_30455509765476285[14] = 0.0;
   out_30455509765476285[15] = 0.0;
   out_30455509765476285[16] = 0.0;
   out_30455509765476285[17] = 0.0;
   out_30455509765476285[18] = 0.0;
   out_30455509765476285[19] = 1.0;
   out_30455509765476285[20] = 0.0;
   out_30455509765476285[21] = 0.0;
   out_30455509765476285[22] = 0.0;
   out_30455509765476285[23] = 0.0;
   out_30455509765476285[24] = 0.0;
   out_30455509765476285[25] = 0.0;
   out_30455509765476285[26] = 0.0;
   out_30455509765476285[27] = 0.0;
   out_30455509765476285[28] = 0.0;
   out_30455509765476285[29] = 0.0;
   out_30455509765476285[30] = 0.0;
   out_30455509765476285[31] = 0.0;
   out_30455509765476285[32] = 0.0;
   out_30455509765476285[33] = 0.0;
   out_30455509765476285[34] = 0.0;
   out_30455509765476285[35] = 0.0;
   out_30455509765476285[36] = 0.0;
   out_30455509765476285[37] = 0.0;
   out_30455509765476285[38] = 1.0;
   out_30455509765476285[39] = 0.0;
   out_30455509765476285[40] = 0.0;
   out_30455509765476285[41] = 0.0;
   out_30455509765476285[42] = 0.0;
   out_30455509765476285[43] = 0.0;
   out_30455509765476285[44] = 0.0;
   out_30455509765476285[45] = 0.0;
   out_30455509765476285[46] = 0.0;
   out_30455509765476285[47] = 0.0;
   out_30455509765476285[48] = 0.0;
   out_30455509765476285[49] = 0.0;
   out_30455509765476285[50] = 0.0;
   out_30455509765476285[51] = 0.0;
   out_30455509765476285[52] = 0.0;
   out_30455509765476285[53] = 0.0;
   out_30455509765476285[54] = 0.0;
   out_30455509765476285[55] = 0.0;
   out_30455509765476285[56] = 0.0;
   out_30455509765476285[57] = 1.0;
   out_30455509765476285[58] = 0.0;
   out_30455509765476285[59] = 0.0;
   out_30455509765476285[60] = 0.0;
   out_30455509765476285[61] = 0.0;
   out_30455509765476285[62] = 0.0;
   out_30455509765476285[63] = 0.0;
   out_30455509765476285[64] = 0.0;
   out_30455509765476285[65] = 0.0;
   out_30455509765476285[66] = 0.0;
   out_30455509765476285[67] = 0.0;
   out_30455509765476285[68] = 0.0;
   out_30455509765476285[69] = 0.0;
   out_30455509765476285[70] = 0.0;
   out_30455509765476285[71] = 0.0;
   out_30455509765476285[72] = 0.0;
   out_30455509765476285[73] = 0.0;
   out_30455509765476285[74] = 0.0;
   out_30455509765476285[75] = 0.0;
   out_30455509765476285[76] = 1.0;
   out_30455509765476285[77] = 0.0;
   out_30455509765476285[78] = 0.0;
   out_30455509765476285[79] = 0.0;
   out_30455509765476285[80] = 0.0;
   out_30455509765476285[81] = 0.0;
   out_30455509765476285[82] = 0.0;
   out_30455509765476285[83] = 0.0;
   out_30455509765476285[84] = 0.0;
   out_30455509765476285[85] = 0.0;
   out_30455509765476285[86] = 0.0;
   out_30455509765476285[87] = 0.0;
   out_30455509765476285[88] = 0.0;
   out_30455509765476285[89] = 0.0;
   out_30455509765476285[90] = 0.0;
   out_30455509765476285[91] = 0.0;
   out_30455509765476285[92] = 0.0;
   out_30455509765476285[93] = 0.0;
   out_30455509765476285[94] = 0.0;
   out_30455509765476285[95] = 1.0;
   out_30455509765476285[96] = 0.0;
   out_30455509765476285[97] = 0.0;
   out_30455509765476285[98] = 0.0;
   out_30455509765476285[99] = 0.0;
   out_30455509765476285[100] = 0.0;
   out_30455509765476285[101] = 0.0;
   out_30455509765476285[102] = 0.0;
   out_30455509765476285[103] = 0.0;
   out_30455509765476285[104] = 0.0;
   out_30455509765476285[105] = 0.0;
   out_30455509765476285[106] = 0.0;
   out_30455509765476285[107] = 0.0;
   out_30455509765476285[108] = 0.0;
   out_30455509765476285[109] = 0.0;
   out_30455509765476285[110] = 0.0;
   out_30455509765476285[111] = 0.0;
   out_30455509765476285[112] = 0.0;
   out_30455509765476285[113] = 0.0;
   out_30455509765476285[114] = 1.0;
   out_30455509765476285[115] = 0.0;
   out_30455509765476285[116] = 0.0;
   out_30455509765476285[117] = 0.0;
   out_30455509765476285[118] = 0.0;
   out_30455509765476285[119] = 0.0;
   out_30455509765476285[120] = 0.0;
   out_30455509765476285[121] = 0.0;
   out_30455509765476285[122] = 0.0;
   out_30455509765476285[123] = 0.0;
   out_30455509765476285[124] = 0.0;
   out_30455509765476285[125] = 0.0;
   out_30455509765476285[126] = 0.0;
   out_30455509765476285[127] = 0.0;
   out_30455509765476285[128] = 0.0;
   out_30455509765476285[129] = 0.0;
   out_30455509765476285[130] = 0.0;
   out_30455509765476285[131] = 0.0;
   out_30455509765476285[132] = 0.0;
   out_30455509765476285[133] = 1.0;
   out_30455509765476285[134] = 0.0;
   out_30455509765476285[135] = 0.0;
   out_30455509765476285[136] = 0.0;
   out_30455509765476285[137] = 0.0;
   out_30455509765476285[138] = 0.0;
   out_30455509765476285[139] = 0.0;
   out_30455509765476285[140] = 0.0;
   out_30455509765476285[141] = 0.0;
   out_30455509765476285[142] = 0.0;
   out_30455509765476285[143] = 0.0;
   out_30455509765476285[144] = 0.0;
   out_30455509765476285[145] = 0.0;
   out_30455509765476285[146] = 0.0;
   out_30455509765476285[147] = 0.0;
   out_30455509765476285[148] = 0.0;
   out_30455509765476285[149] = 0.0;
   out_30455509765476285[150] = 0.0;
   out_30455509765476285[151] = 0.0;
   out_30455509765476285[152] = 1.0;
   out_30455509765476285[153] = 0.0;
   out_30455509765476285[154] = 0.0;
   out_30455509765476285[155] = 0.0;
   out_30455509765476285[156] = 0.0;
   out_30455509765476285[157] = 0.0;
   out_30455509765476285[158] = 0.0;
   out_30455509765476285[159] = 0.0;
   out_30455509765476285[160] = 0.0;
   out_30455509765476285[161] = 0.0;
   out_30455509765476285[162] = 0.0;
   out_30455509765476285[163] = 0.0;
   out_30455509765476285[164] = 0.0;
   out_30455509765476285[165] = 0.0;
   out_30455509765476285[166] = 0.0;
   out_30455509765476285[167] = 0.0;
   out_30455509765476285[168] = 0.0;
   out_30455509765476285[169] = 0.0;
   out_30455509765476285[170] = 0.0;
   out_30455509765476285[171] = 1.0;
   out_30455509765476285[172] = 0.0;
   out_30455509765476285[173] = 0.0;
   out_30455509765476285[174] = 0.0;
   out_30455509765476285[175] = 0.0;
   out_30455509765476285[176] = 0.0;
   out_30455509765476285[177] = 0.0;
   out_30455509765476285[178] = 0.0;
   out_30455509765476285[179] = 0.0;
   out_30455509765476285[180] = 0.0;
   out_30455509765476285[181] = 0.0;
   out_30455509765476285[182] = 0.0;
   out_30455509765476285[183] = 0.0;
   out_30455509765476285[184] = 0.0;
   out_30455509765476285[185] = 0.0;
   out_30455509765476285[186] = 0.0;
   out_30455509765476285[187] = 0.0;
   out_30455509765476285[188] = 0.0;
   out_30455509765476285[189] = 0.0;
   out_30455509765476285[190] = 1.0;
   out_30455509765476285[191] = 0.0;
   out_30455509765476285[192] = 0.0;
   out_30455509765476285[193] = 0.0;
   out_30455509765476285[194] = 0.0;
   out_30455509765476285[195] = 0.0;
   out_30455509765476285[196] = 0.0;
   out_30455509765476285[197] = 0.0;
   out_30455509765476285[198] = 0.0;
   out_30455509765476285[199] = 0.0;
   out_30455509765476285[200] = 0.0;
   out_30455509765476285[201] = 0.0;
   out_30455509765476285[202] = 0.0;
   out_30455509765476285[203] = 0.0;
   out_30455509765476285[204] = 0.0;
   out_30455509765476285[205] = 0.0;
   out_30455509765476285[206] = 0.0;
   out_30455509765476285[207] = 0.0;
   out_30455509765476285[208] = 0.0;
   out_30455509765476285[209] = 1.0;
   out_30455509765476285[210] = 0.0;
   out_30455509765476285[211] = 0.0;
   out_30455509765476285[212] = 0.0;
   out_30455509765476285[213] = 0.0;
   out_30455509765476285[214] = 0.0;
   out_30455509765476285[215] = 0.0;
   out_30455509765476285[216] = 0.0;
   out_30455509765476285[217] = 0.0;
   out_30455509765476285[218] = 0.0;
   out_30455509765476285[219] = 0.0;
   out_30455509765476285[220] = 0.0;
   out_30455509765476285[221] = 0.0;
   out_30455509765476285[222] = 0.0;
   out_30455509765476285[223] = 0.0;
   out_30455509765476285[224] = 0.0;
   out_30455509765476285[225] = 0.0;
   out_30455509765476285[226] = 0.0;
   out_30455509765476285[227] = 0.0;
   out_30455509765476285[228] = 1.0;
   out_30455509765476285[229] = 0.0;
   out_30455509765476285[230] = 0.0;
   out_30455509765476285[231] = 0.0;
   out_30455509765476285[232] = 0.0;
   out_30455509765476285[233] = 0.0;
   out_30455509765476285[234] = 0.0;
   out_30455509765476285[235] = 0.0;
   out_30455509765476285[236] = 0.0;
   out_30455509765476285[237] = 0.0;
   out_30455509765476285[238] = 0.0;
   out_30455509765476285[239] = 0.0;
   out_30455509765476285[240] = 0.0;
   out_30455509765476285[241] = 0.0;
   out_30455509765476285[242] = 0.0;
   out_30455509765476285[243] = 0.0;
   out_30455509765476285[244] = 0.0;
   out_30455509765476285[245] = 0.0;
   out_30455509765476285[246] = 0.0;
   out_30455509765476285[247] = 1.0;
   out_30455509765476285[248] = 0.0;
   out_30455509765476285[249] = 0.0;
   out_30455509765476285[250] = 0.0;
   out_30455509765476285[251] = 0.0;
   out_30455509765476285[252] = 0.0;
   out_30455509765476285[253] = 0.0;
   out_30455509765476285[254] = 0.0;
   out_30455509765476285[255] = 0.0;
   out_30455509765476285[256] = 0.0;
   out_30455509765476285[257] = 0.0;
   out_30455509765476285[258] = 0.0;
   out_30455509765476285[259] = 0.0;
   out_30455509765476285[260] = 0.0;
   out_30455509765476285[261] = 0.0;
   out_30455509765476285[262] = 0.0;
   out_30455509765476285[263] = 0.0;
   out_30455509765476285[264] = 0.0;
   out_30455509765476285[265] = 0.0;
   out_30455509765476285[266] = 1.0;
   out_30455509765476285[267] = 0.0;
   out_30455509765476285[268] = 0.0;
   out_30455509765476285[269] = 0.0;
   out_30455509765476285[270] = 0.0;
   out_30455509765476285[271] = 0.0;
   out_30455509765476285[272] = 0.0;
   out_30455509765476285[273] = 0.0;
   out_30455509765476285[274] = 0.0;
   out_30455509765476285[275] = 0.0;
   out_30455509765476285[276] = 0.0;
   out_30455509765476285[277] = 0.0;
   out_30455509765476285[278] = 0.0;
   out_30455509765476285[279] = 0.0;
   out_30455509765476285[280] = 0.0;
   out_30455509765476285[281] = 0.0;
   out_30455509765476285[282] = 0.0;
   out_30455509765476285[283] = 0.0;
   out_30455509765476285[284] = 0.0;
   out_30455509765476285[285] = 1.0;
   out_30455509765476285[286] = 0.0;
   out_30455509765476285[287] = 0.0;
   out_30455509765476285[288] = 0.0;
   out_30455509765476285[289] = 0.0;
   out_30455509765476285[290] = 0.0;
   out_30455509765476285[291] = 0.0;
   out_30455509765476285[292] = 0.0;
   out_30455509765476285[293] = 0.0;
   out_30455509765476285[294] = 0.0;
   out_30455509765476285[295] = 0.0;
   out_30455509765476285[296] = 0.0;
   out_30455509765476285[297] = 0.0;
   out_30455509765476285[298] = 0.0;
   out_30455509765476285[299] = 0.0;
   out_30455509765476285[300] = 0.0;
   out_30455509765476285[301] = 0.0;
   out_30455509765476285[302] = 0.0;
   out_30455509765476285[303] = 0.0;
   out_30455509765476285[304] = 1.0;
   out_30455509765476285[305] = 0.0;
   out_30455509765476285[306] = 0.0;
   out_30455509765476285[307] = 0.0;
   out_30455509765476285[308] = 0.0;
   out_30455509765476285[309] = 0.0;
   out_30455509765476285[310] = 0.0;
   out_30455509765476285[311] = 0.0;
   out_30455509765476285[312] = 0.0;
   out_30455509765476285[313] = 0.0;
   out_30455509765476285[314] = 0.0;
   out_30455509765476285[315] = 0.0;
   out_30455509765476285[316] = 0.0;
   out_30455509765476285[317] = 0.0;
   out_30455509765476285[318] = 0.0;
   out_30455509765476285[319] = 0.0;
   out_30455509765476285[320] = 0.0;
   out_30455509765476285[321] = 0.0;
   out_30455509765476285[322] = 0.0;
   out_30455509765476285[323] = 1.0;
}
void f_fun(double *state, double dt, double *out_8436412409497388657) {
   out_8436412409497388657[0] = atan2((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), -(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]));
   out_8436412409497388657[1] = asin(sin(dt*state[7])*cos(state[0])*cos(state[1]) - sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1]) + sin(state[1])*cos(dt*state[7])*cos(dt*state[8]));
   out_8436412409497388657[2] = atan2(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), -(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]));
   out_8436412409497388657[3] = dt*state[12] + state[3];
   out_8436412409497388657[4] = dt*state[13] + state[4];
   out_8436412409497388657[5] = dt*state[14] + state[5];
   out_8436412409497388657[6] = state[6];
   out_8436412409497388657[7] = state[7];
   out_8436412409497388657[8] = state[8];
   out_8436412409497388657[9] = state[9];
   out_8436412409497388657[10] = state[10];
   out_8436412409497388657[11] = state[11];
   out_8436412409497388657[12] = state[12];
   out_8436412409497388657[13] = state[13];
   out_8436412409497388657[14] = state[14];
   out_8436412409497388657[15] = state[15];
   out_8436412409497388657[16] = state[16];
   out_8436412409497388657[17] = state[17];
}
void F_fun(double *state, double dt, double *out_7299824336461252759) {
   out_7299824336461252759[0] = ((-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*cos(state[0])*cos(state[1]) - sin(state[0])*cos(dt*state[6])*cos(dt*state[7])*cos(state[1]))*(-(sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) - sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2)) + ((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*cos(state[0])*cos(state[1]) - sin(dt*state[6])*sin(state[0])*cos(dt*state[7])*cos(state[1]))*(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2));
   out_7299824336461252759[1] = ((-sin(dt*state[6])*sin(dt*state[8]) - sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*cos(state[1]) - (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*sin(state[1]) - sin(state[1])*cos(dt*state[6])*cos(dt*state[7])*cos(state[0]))*(-(sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) - sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2)) + (-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))*(-(sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*sin(state[1]) + (-sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) + sin(dt*state[8])*cos(dt*state[6]))*cos(state[1]) - sin(dt*state[6])*sin(state[1])*cos(dt*state[7])*cos(state[0]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2));
   out_7299824336461252759[2] = 0;
   out_7299824336461252759[3] = 0;
   out_7299824336461252759[4] = 0;
   out_7299824336461252759[5] = 0;
   out_7299824336461252759[6] = (-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))*(dt*cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]) + (-dt*sin(dt*state[6])*sin(dt*state[8]) - dt*sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-dt*sin(dt*state[6])*cos(dt*state[8]) + dt*sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2)) + (-(sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) - sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))*(-dt*sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]) + (-dt*sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) - dt*cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (dt*sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - dt*sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2));
   out_7299824336461252759[7] = (-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))*(-dt*sin(dt*state[6])*sin(dt*state[7])*cos(state[0])*cos(state[1]) + dt*sin(dt*state[6])*sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1]) - dt*sin(dt*state[6])*sin(state[1])*cos(dt*state[7])*cos(dt*state[8]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2)) + (-(sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) - sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))*(-dt*sin(dt*state[7])*cos(dt*state[6])*cos(state[0])*cos(state[1]) + dt*sin(dt*state[8])*sin(state[0])*cos(dt*state[6])*cos(dt*state[7])*cos(state[1]) - dt*sin(state[1])*cos(dt*state[6])*cos(dt*state[7])*cos(dt*state[8]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2));
   out_7299824336461252759[8] = ((dt*sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + dt*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (dt*sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - dt*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]))*(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2)) + ((dt*sin(dt*state[6])*sin(dt*state[8]) + dt*sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (-dt*sin(dt*state[6])*cos(dt*state[8]) + dt*sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]))*(-(sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) + (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) - sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]))/(pow(-(sin(dt*state[6])*sin(dt*state[8]) + sin(dt*state[7])*cos(dt*state[6])*cos(dt*state[8]))*sin(state[1]) + (-sin(dt*state[6])*cos(dt*state[8]) + sin(dt*state[7])*sin(dt*state[8])*cos(dt*state[6]))*sin(state[0])*cos(state[1]) + cos(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2) + pow((sin(dt*state[6])*sin(dt*state[7])*sin(dt*state[8]) + cos(dt*state[6])*cos(dt*state[8]))*sin(state[0])*cos(state[1]) - (sin(dt*state[6])*sin(dt*state[7])*cos(dt*state[8]) - sin(dt*state[8])*cos(dt*state[6]))*sin(state[1]) + sin(dt*state[6])*cos(dt*state[7])*cos(state[0])*cos(state[1]), 2));
   out_7299824336461252759[9] = 0;
   out_7299824336461252759[10] = 0;
   out_7299824336461252759[11] = 0;
   out_7299824336461252759[12] = 0;
   out_7299824336461252759[13] = 0;
   out_7299824336461252759[14] = 0;
   out_7299824336461252759[15] = 0;
   out_7299824336461252759[16] = 0;
   out_7299824336461252759[17] = 0;
   out_7299824336461252759[18] = (-sin(dt*state[7])*sin(state[0])*cos(state[1]) - sin(dt*state[8])*cos(dt*state[7])*cos(state[0])*cos(state[1]))/sqrt(1 - pow(sin(dt*state[7])*cos(state[0])*cos(state[1]) - sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1]) + sin(state[1])*cos(dt*state[7])*cos(dt*state[8]), 2));
   out_7299824336461252759[19] = (-sin(dt*state[7])*sin(state[1])*cos(state[0]) + sin(dt*state[8])*sin(state[0])*sin(state[1])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))/sqrt(1 - pow(sin(dt*state[7])*cos(state[0])*cos(state[1]) - sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1]) + sin(state[1])*cos(dt*state[7])*cos(dt*state[8]), 2));
   out_7299824336461252759[20] = 0;
   out_7299824336461252759[21] = 0;
   out_7299824336461252759[22] = 0;
   out_7299824336461252759[23] = 0;
   out_7299824336461252759[24] = 0;
   out_7299824336461252759[25] = (dt*sin(dt*state[7])*sin(dt*state[8])*sin(state[0])*cos(state[1]) - dt*sin(dt*state[7])*sin(state[1])*cos(dt*state[8]) + dt*cos(dt*state[7])*cos(state[0])*cos(state[1]))/sqrt(1 - pow(sin(dt*state[7])*cos(state[0])*cos(state[1]) - sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1]) + sin(state[1])*cos(dt*state[7])*cos(dt*state[8]), 2));
   out_7299824336461252759[26] = (-dt*sin(dt*state[8])*sin(state[1])*cos(dt*state[7]) - dt*sin(state[0])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))/sqrt(1 - pow(sin(dt*state[7])*cos(state[0])*cos(state[1]) - sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1]) + sin(state[1])*cos(dt*state[7])*cos(dt*state[8]), 2));
   out_7299824336461252759[27] = 0;
   out_7299824336461252759[28] = 0;
   out_7299824336461252759[29] = 0;
   out_7299824336461252759[30] = 0;
   out_7299824336461252759[31] = 0;
   out_7299824336461252759[32] = 0;
   out_7299824336461252759[33] = 0;
   out_7299824336461252759[34] = 0;
   out_7299824336461252759[35] = 0;
   out_7299824336461252759[36] = ((sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[7]))*((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) - (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) - sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2)) + ((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[7]))*(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2));
   out_7299824336461252759[37] = (-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))*(-sin(dt*state[7])*sin(state[2])*cos(state[0])*cos(state[1]) + sin(dt*state[8])*sin(state[0])*sin(state[2])*cos(dt*state[7])*cos(state[1]) - sin(state[1])*sin(state[2])*cos(dt*state[7])*cos(dt*state[8]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2)) + ((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) - (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) - sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))*(-sin(dt*state[7])*cos(state[0])*cos(state[1])*cos(state[2]) + sin(dt*state[8])*sin(state[0])*cos(dt*state[7])*cos(state[1])*cos(state[2]) - sin(state[1])*cos(dt*state[7])*cos(dt*state[8])*cos(state[2]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2));
   out_7299824336461252759[38] = ((-sin(state[0])*sin(state[2]) - sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))*(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2)) + ((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (-sin(state[0])*sin(state[1])*sin(state[2]) - cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) - sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))*((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) - (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) - sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2));
   out_7299824336461252759[39] = 0;
   out_7299824336461252759[40] = 0;
   out_7299824336461252759[41] = 0;
   out_7299824336461252759[42] = 0;
   out_7299824336461252759[43] = (-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))*(dt*(sin(state[0])*cos(state[2]) - sin(state[1])*sin(state[2])*cos(state[0]))*cos(dt*state[7]) - dt*(sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[7])*sin(dt*state[8]) - dt*sin(dt*state[7])*sin(state[2])*cos(dt*state[8])*cos(state[1]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2)) + ((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) - (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) - sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))*(dt*(-sin(state[0])*sin(state[2]) - sin(state[1])*cos(state[0])*cos(state[2]))*cos(dt*state[7]) - dt*(sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[7])*sin(dt*state[8]) - dt*sin(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2));
   out_7299824336461252759[44] = (dt*(sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*cos(dt*state[7])*cos(dt*state[8]) - dt*sin(dt*state[8])*sin(state[2])*cos(dt*state[7])*cos(state[1]))*(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2)) + (dt*(sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*cos(dt*state[7])*cos(dt*state[8]) - dt*sin(dt*state[8])*cos(dt*state[7])*cos(state[1])*cos(state[2]))*((-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) - (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) - sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]))/(pow(-(sin(state[0])*sin(state[2]) + sin(state[1])*cos(state[0])*cos(state[2]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*cos(state[2]) - sin(state[2])*cos(state[0]))*sin(dt*state[8])*cos(dt*state[7]) + cos(dt*state[7])*cos(dt*state[8])*cos(state[1])*cos(state[2]), 2) + pow(-(-sin(state[0])*cos(state[2]) + sin(state[1])*sin(state[2])*cos(state[0]))*sin(dt*state[7]) + (sin(state[0])*sin(state[1])*sin(state[2]) + cos(state[0])*cos(state[2]))*sin(dt*state[8])*cos(dt*state[7]) + sin(state[2])*cos(dt*state[7])*cos(dt*state[8])*cos(state[1]), 2));
   out_7299824336461252759[45] = 0;
   out_7299824336461252759[46] = 0;
   out_7299824336461252759[47] = 0;
   out_7299824336461252759[48] = 0;
   out_7299824336461252759[49] = 0;
   out_7299824336461252759[50] = 0;
   out_7299824336461252759[51] = 0;
   out_7299824336461252759[52] = 0;
   out_7299824336461252759[53] = 0;
   out_7299824336461252759[54] = 0;
   out_7299824336461252759[55] = 0;
   out_7299824336461252759[56] = 0;
   out_7299824336461252759[57] = 1;
   out_7299824336461252759[58] = 0;
   out_7299824336461252759[59] = 0;
   out_7299824336461252759[60] = 0;
   out_7299824336461252759[61] = 0;
   out_7299824336461252759[62] = 0;
   out_7299824336461252759[63] = 0;
   out_7299824336461252759[64] = 0;
   out_7299824336461252759[65] = 0;
   out_7299824336461252759[66] = dt;
   out_7299824336461252759[67] = 0;
   out_7299824336461252759[68] = 0;
   out_7299824336461252759[69] = 0;
   out_7299824336461252759[70] = 0;
   out_7299824336461252759[71] = 0;
   out_7299824336461252759[72] = 0;
   out_7299824336461252759[73] = 0;
   out_7299824336461252759[74] = 0;
   out_7299824336461252759[75] = 0;
   out_7299824336461252759[76] = 1;
   out_7299824336461252759[77] = 0;
   out_7299824336461252759[78] = 0;
   out_7299824336461252759[79] = 0;
   out_7299824336461252759[80] = 0;
   out_7299824336461252759[81] = 0;
   out_7299824336461252759[82] = 0;
   out_7299824336461252759[83] = 0;
   out_7299824336461252759[84] = 0;
   out_7299824336461252759[85] = dt;
   out_7299824336461252759[86] = 0;
   out_7299824336461252759[87] = 0;
   out_7299824336461252759[88] = 0;
   out_7299824336461252759[89] = 0;
   out_7299824336461252759[90] = 0;
   out_7299824336461252759[91] = 0;
   out_7299824336461252759[92] = 0;
   out_7299824336461252759[93] = 0;
   out_7299824336461252759[94] = 0;
   out_7299824336461252759[95] = 1;
   out_7299824336461252759[96] = 0;
   out_7299824336461252759[97] = 0;
   out_7299824336461252759[98] = 0;
   out_7299824336461252759[99] = 0;
   out_7299824336461252759[100] = 0;
   out_7299824336461252759[101] = 0;
   out_7299824336461252759[102] = 0;
   out_7299824336461252759[103] = 0;
   out_7299824336461252759[104] = dt;
   out_7299824336461252759[105] = 0;
   out_7299824336461252759[106] = 0;
   out_7299824336461252759[107] = 0;
   out_7299824336461252759[108] = 0;
   out_7299824336461252759[109] = 0;
   out_7299824336461252759[110] = 0;
   out_7299824336461252759[111] = 0;
   out_7299824336461252759[112] = 0;
   out_7299824336461252759[113] = 0;
   out_7299824336461252759[114] = 1;
   out_7299824336461252759[115] = 0;
   out_7299824336461252759[116] = 0;
   out_7299824336461252759[117] = 0;
   out_7299824336461252759[118] = 0;
   out_7299824336461252759[119] = 0;
   out_7299824336461252759[120] = 0;
   out_7299824336461252759[121] = 0;
   out_7299824336461252759[122] = 0;
   out_7299824336461252759[123] = 0;
   out_7299824336461252759[124] = 0;
   out_7299824336461252759[125] = 0;
   out_7299824336461252759[126] = 0;
   out_7299824336461252759[127] = 0;
   out_7299824336461252759[128] = 0;
   out_7299824336461252759[129] = 0;
   out_7299824336461252759[130] = 0;
   out_7299824336461252759[131] = 0;
   out_7299824336461252759[132] = 0;
   out_7299824336461252759[133] = 1;
   out_7299824336461252759[134] = 0;
   out_7299824336461252759[135] = 0;
   out_7299824336461252759[136] = 0;
   out_7299824336461252759[137] = 0;
   out_7299824336461252759[138] = 0;
   out_7299824336461252759[139] = 0;
   out_7299824336461252759[140] = 0;
   out_7299824336461252759[141] = 0;
   out_7299824336461252759[142] = 0;
   out_7299824336461252759[143] = 0;
   out_7299824336461252759[144] = 0;
   out_7299824336461252759[145] = 0;
   out_7299824336461252759[146] = 0;
   out_7299824336461252759[147] = 0;
   out_7299824336461252759[148] = 0;
   out_7299824336461252759[149] = 0;
   out_7299824336461252759[150] = 0;
   out_7299824336461252759[151] = 0;
   out_7299824336461252759[152] = 1;
   out_7299824336461252759[153] = 0;
   out_7299824336461252759[154] = 0;
   out_7299824336461252759[155] = 0;
   out_7299824336461252759[156] = 0;
   out_7299824336461252759[157] = 0;
   out_7299824336461252759[158] = 0;
   out_7299824336461252759[159] = 0;
   out_7299824336461252759[160] = 0;
   out_7299824336461252759[161] = 0;
   out_7299824336461252759[162] = 0;
   out_7299824336461252759[163] = 0;
   out_7299824336461252759[164] = 0;
   out_7299824336461252759[165] = 0;
   out_7299824336461252759[166] = 0;
   out_7299824336461252759[167] = 0;
   out_7299824336461252759[168] = 0;
   out_7299824336461252759[169] = 0;
   out_7299824336461252759[170] = 0;
   out_7299824336461252759[171] = 1;
   out_7299824336461252759[172] = 0;
   out_7299824336461252759[173] = 0;
   out_7299824336461252759[174] = 0;
   out_7299824336461252759[175] = 0;
   out_7299824336461252759[176] = 0;
   out_7299824336461252759[177] = 0;
   out_7299824336461252759[178] = 0;
   out_7299824336461252759[179] = 0;
   out_7299824336461252759[180] = 0;
   out_7299824336461252759[181] = 0;
   out_7299824336461252759[182] = 0;
   out_7299824336461252759[183] = 0;
   out_7299824336461252759[184] = 0;
   out_7299824336461252759[185] = 0;
   out_7299824336461252759[186] = 0;
   out_7299824336461252759[187] = 0;
   out_7299824336461252759[188] = 0;
   out_7299824336461252759[189] = 0;
   out_7299824336461252759[190] = 1;
   out_7299824336461252759[191] = 0;
   out_7299824336461252759[192] = 0;
   out_7299824336461252759[193] = 0;
   out_7299824336461252759[194] = 0;
   out_7299824336461252759[195] = 0;
   out_7299824336461252759[196] = 0;
   out_7299824336461252759[197] = 0;
   out_7299824336461252759[198] = 0;
   out_7299824336461252759[199] = 0;
   out_7299824336461252759[200] = 0;
   out_7299824336461252759[201] = 0;
   out_7299824336461252759[202] = 0;
   out_7299824336461252759[203] = 0;
   out_7299824336461252759[204] = 0;
   out_7299824336461252759[205] = 0;
   out_7299824336461252759[206] = 0;
   out_7299824336461252759[207] = 0;
   out_7299824336461252759[208] = 0;
   out_7299824336461252759[209] = 1;
   out_7299824336461252759[210] = 0;
   out_7299824336461252759[211] = 0;
   out_7299824336461252759[212] = 0;
   out_7299824336461252759[213] = 0;
   out_7299824336461252759[214] = 0;
   out_7299824336461252759[215] = 0;
   out_7299824336461252759[216] = 0;
   out_7299824336461252759[217] = 0;
   out_7299824336461252759[218] = 0;
   out_7299824336461252759[219] = 0;
   out_7299824336461252759[220] = 0;
   out_7299824336461252759[221] = 0;
   out_7299824336461252759[222] = 0;
   out_7299824336461252759[223] = 0;
   out_7299824336461252759[224] = 0;
   out_7299824336461252759[225] = 0;
   out_7299824336461252759[226] = 0;
   out_7299824336461252759[227] = 0;
   out_7299824336461252759[228] = 1;
   out_7299824336461252759[229] = 0;
   out_7299824336461252759[230] = 0;
   out_7299824336461252759[231] = 0;
   out_7299824336461252759[232] = 0;
   out_7299824336461252759[233] = 0;
   out_7299824336461252759[234] = 0;
   out_7299824336461252759[235] = 0;
   out_7299824336461252759[236] = 0;
   out_7299824336461252759[237] = 0;
   out_7299824336461252759[238] = 0;
   out_7299824336461252759[239] = 0;
   out_7299824336461252759[240] = 0;
   out_7299824336461252759[241] = 0;
   out_7299824336461252759[242] = 0;
   out_7299824336461252759[243] = 0;
   out_7299824336461252759[244] = 0;
   out_7299824336461252759[245] = 0;
   out_7299824336461252759[246] = 0;
   out_7299824336461252759[247] = 1;
   out_7299824336461252759[248] = 0;
   out_7299824336461252759[249] = 0;
   out_7299824336461252759[250] = 0;
   out_7299824336461252759[251] = 0;
   out_7299824336461252759[252] = 0;
   out_7299824336461252759[253] = 0;
   out_7299824336461252759[254] = 0;
   out_7299824336461252759[255] = 0;
   out_7299824336461252759[256] = 0;
   out_7299824336461252759[257] = 0;
   out_7299824336461252759[258] = 0;
   out_7299824336461252759[259] = 0;
   out_7299824336461252759[260] = 0;
   out_7299824336461252759[261] = 0;
   out_7299824336461252759[262] = 0;
   out_7299824336461252759[263] = 0;
   out_7299824336461252759[264] = 0;
   out_7299824336461252759[265] = 0;
   out_7299824336461252759[266] = 1;
   out_7299824336461252759[267] = 0;
   out_7299824336461252759[268] = 0;
   out_7299824336461252759[269] = 0;
   out_7299824336461252759[270] = 0;
   out_7299824336461252759[271] = 0;
   out_7299824336461252759[272] = 0;
   out_7299824336461252759[273] = 0;
   out_7299824336461252759[274] = 0;
   out_7299824336461252759[275] = 0;
   out_7299824336461252759[276] = 0;
   out_7299824336461252759[277] = 0;
   out_7299824336461252759[278] = 0;
   out_7299824336461252759[279] = 0;
   out_7299824336461252759[280] = 0;
   out_7299824336461252759[281] = 0;
   out_7299824336461252759[282] = 0;
   out_7299824336461252759[283] = 0;
   out_7299824336461252759[284] = 0;
   out_7299824336461252759[285] = 1;
   out_7299824336461252759[286] = 0;
   out_7299824336461252759[287] = 0;
   out_7299824336461252759[288] = 0;
   out_7299824336461252759[289] = 0;
   out_7299824336461252759[290] = 0;
   out_7299824336461252759[291] = 0;
   out_7299824336461252759[292] = 0;
   out_7299824336461252759[293] = 0;
   out_7299824336461252759[294] = 0;
   out_7299824336461252759[295] = 0;
   out_7299824336461252759[296] = 0;
   out_7299824336461252759[297] = 0;
   out_7299824336461252759[298] = 0;
   out_7299824336461252759[299] = 0;
   out_7299824336461252759[300] = 0;
   out_7299824336461252759[301] = 0;
   out_7299824336461252759[302] = 0;
   out_7299824336461252759[303] = 0;
   out_7299824336461252759[304] = 1;
   out_7299824336461252759[305] = 0;
   out_7299824336461252759[306] = 0;
   out_7299824336461252759[307] = 0;
   out_7299824336461252759[308] = 0;
   out_7299824336461252759[309] = 0;
   out_7299824336461252759[310] = 0;
   out_7299824336461252759[311] = 0;
   out_7299824336461252759[312] = 0;
   out_7299824336461252759[313] = 0;
   out_7299824336461252759[314] = 0;
   out_7299824336461252759[315] = 0;
   out_7299824336461252759[316] = 0;
   out_7299824336461252759[317] = 0;
   out_7299824336461252759[318] = 0;
   out_7299824336461252759[319] = 0;
   out_7299824336461252759[320] = 0;
   out_7299824336461252759[321] = 0;
   out_7299824336461252759[322] = 0;
   out_7299824336461252759[323] = 1;
}
void h_4(double *state, double *unused, double *out_6774107777547476022) {
   out_6774107777547476022[0] = state[6] + state[9];
   out_6774107777547476022[1] = state[7] + state[10];
   out_6774107777547476022[2] = state[8] + state[11];
}
void H_4(double *state, double *unused, double *out_1025057040572079440) {
   out_1025057040572079440[0] = 0;
   out_1025057040572079440[1] = 0;
   out_1025057040572079440[2] = 0;
   out_1025057040572079440[3] = 0;
   out_1025057040572079440[4] = 0;
   out_1025057040572079440[5] = 0;
   out_1025057040572079440[6] = 1;
   out_1025057040572079440[7] = 0;
   out_1025057040572079440[8] = 0;
   out_1025057040572079440[9] = 1;
   out_1025057040572079440[10] = 0;
   out_1025057040572079440[11] = 0;
   out_1025057040572079440[12] = 0;
   out_1025057040572079440[13] = 0;
   out_1025057040572079440[14] = 0;
   out_1025057040572079440[15] = 0;
   out_1025057040572079440[16] = 0;
   out_1025057040572079440[17] = 0;
   out_1025057040572079440[18] = 0;
   out_1025057040572079440[19] = 0;
   out_1025057040572079440[20] = 0;
   out_1025057040572079440[21] = 0;
   out_1025057040572079440[22] = 0;
   out_1025057040572079440[23] = 0;
   out_1025057040572079440[24] = 0;
   out_1025057040572079440[25] = 1;
   out_1025057040572079440[26] = 0;
   out_1025057040572079440[27] = 0;
   out_1025057040572079440[28] = 1;
   out_1025057040572079440[29] = 0;
   out_1025057040572079440[30] = 0;
   out_1025057040572079440[31] = 0;
   out_1025057040572079440[32] = 0;
   out_1025057040572079440[33] = 0;
   out_1025057040572079440[34] = 0;
   out_1025057040572079440[35] = 0;
   out_1025057040572079440[36] = 0;
   out_1025057040572079440[37] = 0;
   out_1025057040572079440[38] = 0;
   out_1025057040572079440[39] = 0;
   out_1025057040572079440[40] = 0;
   out_1025057040572079440[41] = 0;
   out_1025057040572079440[42] = 0;
   out_1025057040572079440[43] = 0;
   out_1025057040572079440[44] = 1;
   out_1025057040572079440[45] = 0;
   out_1025057040572079440[46] = 0;
   out_1025057040572079440[47] = 1;
   out_1025057040572079440[48] = 0;
   out_1025057040572079440[49] = 0;
   out_1025057040572079440[50] = 0;
   out_1025057040572079440[51] = 0;
   out_1025057040572079440[52] = 0;
   out_1025057040572079440[53] = 0;
}
void h_10(double *state, double *unused, double *out_5889283512516384374) {
   out_5889283512516384374[0] = 9.8100000000000005*sin(state[1]) - state[4]*state[8] + state[5]*state[7] + state[12] + state[15];
   out_5889283512516384374[1] = -9.8100000000000005*sin(state[0])*cos(state[1]) + state[3]*state[8] - state[5]*state[6] + state[13] + state[16];
   out_5889283512516384374[2] = -9.8100000000000005*cos(state[0])*cos(state[1]) - state[3]*state[7] + state[4]*state[6] + state[14] + state[17];
}
void H_10(double *state, double *unused, double *out_1898548047416842289) {
   out_1898548047416842289[0] = 0;
   out_1898548047416842289[1] = 9.8100000000000005*cos(state[1]);
   out_1898548047416842289[2] = 0;
   out_1898548047416842289[3] = 0;
   out_1898548047416842289[4] = -state[8];
   out_1898548047416842289[5] = state[7];
   out_1898548047416842289[6] = 0;
   out_1898548047416842289[7] = state[5];
   out_1898548047416842289[8] = -state[4];
   out_1898548047416842289[9] = 0;
   out_1898548047416842289[10] = 0;
   out_1898548047416842289[11] = 0;
   out_1898548047416842289[12] = 1;
   out_1898548047416842289[13] = 0;
   out_1898548047416842289[14] = 0;
   out_1898548047416842289[15] = 1;
   out_1898548047416842289[16] = 0;
   out_1898548047416842289[17] = 0;
   out_1898548047416842289[18] = -9.8100000000000005*cos(state[0])*cos(state[1]);
   out_1898548047416842289[19] = 9.8100000000000005*sin(state[0])*sin(state[1]);
   out_1898548047416842289[20] = 0;
   out_1898548047416842289[21] = state[8];
   out_1898548047416842289[22] = 0;
   out_1898548047416842289[23] = -state[6];
   out_1898548047416842289[24] = -state[5];
   out_1898548047416842289[25] = 0;
   out_1898548047416842289[26] = state[3];
   out_1898548047416842289[27] = 0;
   out_1898548047416842289[28] = 0;
   out_1898548047416842289[29] = 0;
   out_1898548047416842289[30] = 0;
   out_1898548047416842289[31] = 1;
   out_1898548047416842289[32] = 0;
   out_1898548047416842289[33] = 0;
   out_1898548047416842289[34] = 1;
   out_1898548047416842289[35] = 0;
   out_1898548047416842289[36] = 9.8100000000000005*sin(state[0])*cos(state[1]);
   out_1898548047416842289[37] = 9.8100000000000005*sin(state[1])*cos(state[0]);
   out_1898548047416842289[38] = 0;
   out_1898548047416842289[39] = -state[7];
   out_1898548047416842289[40] = state[6];
   out_1898548047416842289[41] = 0;
   out_1898548047416842289[42] = state[4];
   out_1898548047416842289[43] = -state[3];
   out_1898548047416842289[44] = 0;
   out_1898548047416842289[45] = 0;
   out_1898548047416842289[46] = 0;
   out_1898548047416842289[47] = 0;
   out_1898548047416842289[48] = 0;
   out_1898548047416842289[49] = 0;
   out_1898548047416842289[50] = 1;
   out_1898548047416842289[51] = 0;
   out_1898548047416842289[52] = 0;
   out_1898548047416842289[53] = 1;
}
void h_13(double *state, double *unused, double *out_2892049699464882933) {
   out_2892049699464882933[0] = state[3];
   out_2892049699464882933[1] = state[4];
   out_2892049699464882933[2] = state[5];
}
void H_13(double *state, double *unused, double *out_2187216784760253361) {
   out_2187216784760253361[0] = 0;
   out_2187216784760253361[1] = 0;
   out_2187216784760253361[2] = 0;
   out_2187216784760253361[3] = 1;
   out_2187216784760253361[4] = 0;
   out_2187216784760253361[5] = 0;
   out_2187216784760253361[6] = 0;
   out_2187216784760253361[7] = 0;
   out_2187216784760253361[8] = 0;
   out_2187216784760253361[9] = 0;
   out_2187216784760253361[10] = 0;
   out_2187216784760253361[11] = 0;
   out_2187216784760253361[12] = 0;
   out_2187216784760253361[13] = 0;
   out_2187216784760253361[14] = 0;
   out_2187216784760253361[15] = 0;
   out_2187216784760253361[16] = 0;
   out_2187216784760253361[17] = 0;
   out_2187216784760253361[18] = 0;
   out_2187216784760253361[19] = 0;
   out_2187216784760253361[20] = 0;
   out_2187216784760253361[21] = 0;
   out_2187216784760253361[22] = 1;
   out_2187216784760253361[23] = 0;
   out_2187216784760253361[24] = 0;
   out_2187216784760253361[25] = 0;
   out_2187216784760253361[26] = 0;
   out_2187216784760253361[27] = 0;
   out_2187216784760253361[28] = 0;
   out_2187216784760253361[29] = 0;
   out_2187216784760253361[30] = 0;
   out_2187216784760253361[31] = 0;
   out_2187216784760253361[32] = 0;
   out_2187216784760253361[33] = 0;
   out_2187216784760253361[34] = 0;
   out_2187216784760253361[35] = 0;
   out_2187216784760253361[36] = 0;
   out_2187216784760253361[37] = 0;
   out_2187216784760253361[38] = 0;
   out_2187216784760253361[39] = 0;
   out_2187216784760253361[40] = 0;
   out_2187216784760253361[41] = 1;
   out_2187216784760253361[42] = 0;
   out_2187216784760253361[43] = 0;
   out_2187216784760253361[44] = 0;
   out_2187216784760253361[45] = 0;
   out_2187216784760253361[46] = 0;
   out_2187216784760253361[47] = 0;
   out_2187216784760253361[48] = 0;
   out_2187216784760253361[49] = 0;
   out_2187216784760253361[50] = 0;
   out_2187216784760253361[51] = 0;
   out_2187216784760253361[52] = 0;
   out_2187216784760253361[53] = 0;
}
void h_14(double *state, double *unused, double *out_1913422395156722151) {
   out_1913422395156722151[0] = state[6];
   out_1913422395156722151[1] = state[7];
   out_1913422395156722151[2] = state[8];
}
void H_14(double *state, double *unused, double *out_4107845472867451736) {
   out_4107845472867451736[0] = 0;
   out_4107845472867451736[1] = 0;
   out_4107845472867451736[2] = 0;
   out_4107845472867451736[3] = 0;
   out_4107845472867451736[4] = 0;
   out_4107845472867451736[5] = 0;
   out_4107845472867451736[6] = 1;
   out_4107845472867451736[7] = 0;
   out_4107845472867451736[8] = 0;
   out_4107845472867451736[9] = 0;
   out_4107845472867451736[10] = 0;
   out_4107845472867451736[11] = 0;
   out_4107845472867451736[12] = 0;
   out_4107845472867451736[13] = 0;
   out_4107845472867451736[14] = 0;
   out_4107845472867451736[15] = 0;
   out_4107845472867451736[16] = 0;
   out_4107845472867451736[17] = 0;
   out_4107845472867451736[18] = 0;
   out_4107845472867451736[19] = 0;
   out_4107845472867451736[20] = 0;
   out_4107845472867451736[21] = 0;
   out_4107845472867451736[22] = 0;
   out_4107845472867451736[23] = 0;
   out_4107845472867451736[24] = 0;
   out_4107845472867451736[25] = 1;
   out_4107845472867451736[26] = 0;
   out_4107845472867451736[27] = 0;
   out_4107845472867451736[28] = 0;
   out_4107845472867451736[29] = 0;
   out_4107845472867451736[30] = 0;
   out_4107845472867451736[31] = 0;
   out_4107845472867451736[32] = 0;
   out_4107845472867451736[33] = 0;
   out_4107845472867451736[34] = 0;
   out_4107845472867451736[35] = 0;
   out_4107845472867451736[36] = 0;
   out_4107845472867451736[37] = 0;
   out_4107845472867451736[38] = 0;
   out_4107845472867451736[39] = 0;
   out_4107845472867451736[40] = 0;
   out_4107845472867451736[41] = 0;
   out_4107845472867451736[42] = 0;
   out_4107845472867451736[43] = 0;
   out_4107845472867451736[44] = 1;
   out_4107845472867451736[45] = 0;
   out_4107845472867451736[46] = 0;
   out_4107845472867451736[47] = 0;
   out_4107845472867451736[48] = 0;
   out_4107845472867451736[49] = 0;
   out_4107845472867451736[50] = 0;
   out_4107845472867451736[51] = 0;
   out_4107845472867451736[52] = 0;
   out_4107845472867451736[53] = 0;
}
#include <eigen3/Eigen/Dense>
#include <iostream>

typedef Eigen::Matrix<double, DIM, DIM, Eigen::RowMajor> DDM;
typedef Eigen::Matrix<double, EDIM, EDIM, Eigen::RowMajor> EEM;
typedef Eigen::Matrix<double, DIM, EDIM, Eigen::RowMajor> DEM;

void predict(double *in_x, double *in_P, double *in_Q, double dt) {
  typedef Eigen::Matrix<double, MEDIM, MEDIM, Eigen::RowMajor> RRM;

  double nx[DIM] = {0};
  double in_F[EDIM*EDIM] = {0};

  // functions from sympy
  f_fun(in_x, dt, nx);
  F_fun(in_x, dt, in_F);


  EEM F(in_F);
  EEM P(in_P);
  EEM Q(in_Q);

  RRM F_main = F.topLeftCorner(MEDIM, MEDIM);
  P.topLeftCorner(MEDIM, MEDIM) = (F_main * P.topLeftCorner(MEDIM, MEDIM)) * F_main.transpose();
  P.topRightCorner(MEDIM, EDIM - MEDIM) = F_main * P.topRightCorner(MEDIM, EDIM - MEDIM);
  P.bottomLeftCorner(EDIM - MEDIM, MEDIM) = P.bottomLeftCorner(EDIM - MEDIM, MEDIM) * F_main.transpose();

  P = P + dt*Q;

  // copy out state
  memcpy(in_x, nx, DIM * sizeof(double));
  memcpy(in_P, P.data(), EDIM * EDIM * sizeof(double));
}

// note: extra_args dim only correct when null space projecting
// otherwise 1
template <int ZDIM, int EADIM, bool MAHA_TEST>
void update(double *in_x, double *in_P, Hfun h_fun, Hfun H_fun, Hfun Hea_fun, double *in_z, double *in_R, double *in_ea, double MAHA_THRESHOLD) {
  typedef Eigen::Matrix<double, ZDIM, ZDIM, Eigen::RowMajor> ZZM;
  typedef Eigen::Matrix<double, ZDIM, DIM, Eigen::RowMajor> ZDM;
  typedef Eigen::Matrix<double, Eigen::Dynamic, EDIM, Eigen::RowMajor> XEM;
  //typedef Eigen::Matrix<double, EDIM, ZDIM, Eigen::RowMajor> EZM;
  typedef Eigen::Matrix<double, Eigen::Dynamic, 1> X1M;
  typedef Eigen::Matrix<double, Eigen::Dynamic, Eigen::Dynamic, Eigen::RowMajor> XXM;

  double in_hx[ZDIM] = {0};
  double in_H[ZDIM * DIM] = {0};
  double in_H_mod[EDIM * DIM] = {0};
  double delta_x[EDIM] = {0};
  double x_new[DIM] = {0};


  // state x, P
  Eigen::Matrix<double, ZDIM, 1> z(in_z);
  EEM P(in_P);
  ZZM pre_R(in_R);

  // functions from sympy
  h_fun(in_x, in_ea, in_hx);
  H_fun(in_x, in_ea, in_H);
  ZDM pre_H(in_H);

  // get y (y = z - hx)
  Eigen::Matrix<double, ZDIM, 1> pre_y(in_hx); pre_y = z - pre_y;
  X1M y; XXM H; XXM R;
  if (Hea_fun){
    typedef Eigen::Matrix<double, ZDIM, EADIM, Eigen::RowMajor> ZAM;
    double in_Hea[ZDIM * EADIM] = {0};
    Hea_fun(in_x, in_ea, in_Hea);
    ZAM Hea(in_Hea);
    XXM A = Hea.transpose().fullPivLu().kernel();


    y = A.transpose() * pre_y;
    H = A.transpose() * pre_H;
    R = A.transpose() * pre_R * A;
  } else {
    y = pre_y;
    H = pre_H;
    R = pre_R;
  }
  // get modified H
  H_mod_fun(in_x, in_H_mod);
  DEM H_mod(in_H_mod);
  XEM H_err = H * H_mod;

  // Do mahalobis distance test
  if (MAHA_TEST){
    XXM a = (H_err * P * H_err.transpose() + R).inverse();
    double maha_dist = y.transpose() * a * y;
    if (maha_dist > MAHA_THRESHOLD){
      R = 1.0e16 * R;
    }
  }

  // Outlier resilient weighting
  double weight = 1;//(1.5)/(1 + y.squaredNorm()/R.sum());

  // kalman gains and I_KH
  XXM S = ((H_err * P) * H_err.transpose()) + R/weight;
  XEM KT = S.fullPivLu().solve(H_err * P.transpose());
  //EZM K = KT.transpose(); TODO: WHY DOES THIS NOT COMPILE?
  //EZM K = S.fullPivLu().solve(H_err * P.transpose()).transpose();
  //std::cout << "Here is the matrix rot:\n" << K << std::endl;
  EEM I_KH = Eigen::Matrix<double, EDIM, EDIM>::Identity() - (KT.transpose() * H_err);

  // update state by injecting dx
  Eigen::Matrix<double, EDIM, 1> dx(delta_x);
  dx  = (KT.transpose() * y);
  memcpy(delta_x, dx.data(), EDIM * sizeof(double));
  err_fun(in_x, delta_x, x_new);
  Eigen::Matrix<double, DIM, 1> x(x_new);

  // update cov
  P = ((I_KH * P) * I_KH.transpose()) + ((KT.transpose() * R) * KT);

  // copy out state
  memcpy(in_x, x.data(), DIM * sizeof(double));
  memcpy(in_P, P.data(), EDIM * EDIM * sizeof(double));
  memcpy(in_z, y.data(), y.rows() * sizeof(double));
}




}
extern "C" {

void pose_update_4(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea) {
  update<3, 3, 0>(in_x, in_P, h_4, H_4, NULL, in_z, in_R, in_ea, MAHA_THRESH_4);
}
void pose_update_10(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea) {
  update<3, 3, 0>(in_x, in_P, h_10, H_10, NULL, in_z, in_R, in_ea, MAHA_THRESH_10);
}
void pose_update_13(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea) {
  update<3, 3, 0>(in_x, in_P, h_13, H_13, NULL, in_z, in_R, in_ea, MAHA_THRESH_13);
}
void pose_update_14(double *in_x, double *in_P, double *in_z, double *in_R, double *in_ea) {
  update<3, 3, 0>(in_x, in_P, h_14, H_14, NULL, in_z, in_R, in_ea, MAHA_THRESH_14);
}
void pose_err_fun(double *nom_x, double *delta_x, double *out_4922672802559952531) {
  err_fun(nom_x, delta_x, out_4922672802559952531);
}
void pose_inv_err_fun(double *nom_x, double *true_x, double *out_4675286663206909820) {
  inv_err_fun(nom_x, true_x, out_4675286663206909820);
}
void pose_H_mod_fun(double *state, double *out_30455509765476285) {
  H_mod_fun(state, out_30455509765476285);
}
void pose_f_fun(double *state, double dt, double *out_8436412409497388657) {
  f_fun(state,  dt, out_8436412409497388657);
}
void pose_F_fun(double *state, double dt, double *out_7299824336461252759) {
  F_fun(state,  dt, out_7299824336461252759);
}
void pose_h_4(double *state, double *unused, double *out_6774107777547476022) {
  h_4(state, unused, out_6774107777547476022);
}
void pose_H_4(double *state, double *unused, double *out_1025057040572079440) {
  H_4(state, unused, out_1025057040572079440);
}
void pose_h_10(double *state, double *unused, double *out_5889283512516384374) {
  h_10(state, unused, out_5889283512516384374);
}
void pose_H_10(double *state, double *unused, double *out_1898548047416842289) {
  H_10(state, unused, out_1898548047416842289);
}
void pose_h_13(double *state, double *unused, double *out_2892049699464882933) {
  h_13(state, unused, out_2892049699464882933);
}
void pose_H_13(double *state, double *unused, double *out_2187216784760253361) {
  H_13(state, unused, out_2187216784760253361);
}
void pose_h_14(double *state, double *unused, double *out_1913422395156722151) {
  h_14(state, unused, out_1913422395156722151);
}
void pose_H_14(double *state, double *unused, double *out_4107845472867451736) {
  H_14(state, unused, out_4107845472867451736);
}
void pose_predict(double *in_x, double *in_P, double *in_Q, double dt) {
  predict(in_x, in_P, in_Q, dt);
}
}

const EKF pose = {
  .name = "pose",
  .kinds = { 4, 10, 13, 14 },
  .feature_kinds = {  },
  .f_fun = pose_f_fun,
  .F_fun = pose_F_fun,
  .err_fun = pose_err_fun,
  .inv_err_fun = pose_inv_err_fun,
  .H_mod_fun = pose_H_mod_fun,
  .predict = pose_predict,
  .hs = {
    { 4, pose_h_4 },
    { 10, pose_h_10 },
    { 13, pose_h_13 },
    { 14, pose_h_14 },
  },
  .Hs = {
    { 4, pose_H_4 },
    { 10, pose_H_10 },
    { 13, pose_H_13 },
    { 14, pose_H_14 },
  },
  .updates = {
    { 4, pose_update_4 },
    { 10, pose_update_10 },
    { 13, pose_update_13 },
    { 14, pose_update_14 },
  },
  .Hes = {
  },
  .sets = {
  },
  .extra_routines = {
  },
};

ekf_lib_init(pose)
